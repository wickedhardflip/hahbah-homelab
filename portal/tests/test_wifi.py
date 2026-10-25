"""Wi-Fi history, summary, neighbor scan parsing, interference, and the page API. Files only, no network."""
import json
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import presence, wifi
from app.main import create_app
from app.models import User
from conftest import FakeBuilder, make_user, sign_in

STATIC = Path(__file__).parent.parent / "app" / "static"
OK = {"origin": "https://home.example.com"}
BAD = {"origin": "https://evil.example"}
T0 = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)
PHONE, TV = "a1a1a1a1a1", "c3c3c3c3c3"
TZ = ZoneInfo("America/New_York")
MAC = r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}"

NETSH = """
SSID 1 : Neighbor One
    Network type            : Infrastructure
    BSSID 1                 : aa:bb:cc:dd:ee:01
         Signal             : 80%
         Channel            : 6
SSID 2 : Neighbor Two
    BSSID 1                 : aa:bb:cc:dd:ee:02
         Signal             : 40%
         Channel            : 6
    BSSID 2                 : aa:bb:cc:dd:ee:03
         Signal             : 60%
         Channel            : 44
SSID 3 :
    BSSID 1                 : aa:bb:cc:dd:ee:04
         Signal             : 50%
         Channel            : 5
"""


def write_eero(out, at, clients, channel=6, util=30, node_status="online"):
    out.mkdir(parents=True, exist_ok=True)
    data = {"clients": clients, "nodes": [{"name": "Living room", "model": "eero 6", "status": node_status, "mesh_bars": 5,
                                           "channels": {"band_2GHz": {"channel": channel, "width": 20, "utilization": util, "clients": 4}}}]}
    (out / "eero.json").write_text(json.dumps({"checked_at": presence.iso(at), "interval_s": 300, "ok": True, "data": data}))


def client(i, dbm_text="-52 dBm", node="Living room", band="5 GHz", wired=False):
    return {"id": i, "name": "Phone " + i[:2], "band": band, "signal_dbm": dbm_text, "node": node, "wired": wired}


def tracker(tmp_path, at):
    return wifi.WifiTracker(tmp_path, now=lambda: at)


def test_signal_and_band_parsing():
    assert wifi.dbm("-52 dBm") == -52 and wifi.dbm(-60) == -60 and wifi.dbm("n/a") is None and wifi.dbm(5) is None and wifi.dbm(True) is None
    assert wifi.band_of("band_5GHz_full") == "5 GHz" and wifi.band_of("band_2GHz") == "2.4 GHz" and wifi.band_of("2.4 GHz") == "2.4 GHz"
    assert wifi.band_of(None) is None and wifi.quality(-60) == "good" and wifi.quality(-70) == "fair" and wifi.quality(-80) == "weak"


def test_tick_records_wireless_only_and_is_quiet_without_new_data(tmp_path):
    write_eero(tmp_path, T0, [client(PHONE), client(TV, wired=True), client("b2b2b2b2b2", dbm_text=None)])
    t = tracker(tmp_path, T0)
    st = t.tick()
    assert list(st["devices"]) == [PHONE] and st["devices"][PHONE]["pts"] == [[int(T0.timestamp()), -52, 1]]
    assert st["nodes"]["Living room"]["radios"]["2.4 GHz"]["channel"] == 6
    assert t.tick() is None   # same Eero run: nothing new


def test_tick_unknown_when_eero_is_old_or_missing(tmp_path):
    t = tracker(tmp_path, T0 + timedelta(hours=2))
    assert t.tick() is None   # no file
    write_eero(tmp_path, T0, [client(PHONE)])
    assert t.tick() is None   # too old to trust


def test_roam_band_and_channel_events_but_not_after_a_long_gap(tmp_path):
    write_eero(tmp_path, T0, [client(PHONE)])
    t = tracker(tmp_path, T0)
    t.tick()
    at = T0 + timedelta(minutes=5)
    t.now = lambda: at
    write_eero(tmp_path, at, [client(PHONE, node="Bedroom", band="2.4 GHz", dbm_text="-70 dBm")], channel=11, node_status="offline")
    st = t.tick()
    assert sorted(e[1] for e in st["events"]) == ["band", "channel", "node", "roam"]
    late = at + timedelta(hours=3)
    t.now = lambda: late
    write_eero(tmp_path, late, [client(PHONE, node="Living room")], channel=11, node_status="offline")
    assert len(t.tick()["events"]) == 4   # a reconnect hours later is not a roam


def test_history_is_trimmed_to_seven_days_and_restores_from_disk(tmp_path):
    write_eero(tmp_path, T0, [client(PHONE)])
    t = tracker(tmp_path, T0)
    (tmp_path / "wifi.json").write_text(json.dumps({"data": t.tick()}))
    later = T0 + timedelta(days=8)
    write_eero(tmp_path, later, [client(TV, node="Bedroom")])
    t2 = tracker(tmp_path, later)
    assert PHONE in t2.state["devices"]
    assert list(t2.tick()["devices"]) == [TV]   # the old phone's points all expired


def summary(tmp_path, admin=True, now=None):
    write_eero(tmp_path, T0, [client(PHONE), client(TV, "-80 dBm", band="2.4 GHz")])
    st = tracker(tmp_path, T0).tick()
    return wifi.summarize(st, now or T0 + timedelta(minutes=2), TZ, admin)


def test_summary_admin_vs_member(tmp_path):
    a = summary(tmp_path, True)
    assert [d["id"] for d in a["devices"]] == [TV, PHONE]   # weakest first
    assert a["devices"][0]["quality"] == "weak" and a["weak"][0]["id"] == TV and not a["stale"]
    assert len(a["devices"][1]["hod"]) == 24 and a["devices"][1]["hourly"]
    m = summary(tmp_path, False)
    assert "devices" not in m and m["nodes"][0]["radios"]["2.4 GHz"]["utilization"] == 30


def test_summary_marks_stale_and_not_connected(tmp_path):
    s = summary(tmp_path, True, now=T0 + timedelta(hours=3))
    assert s["stale"] and all(not d["connected"] and d["dbm"] is None and d["quality"] == "none" for d in s["devices"])


def test_summary_survives_an_empty_state():
    s = wifi.summarize(dict(wifi.EMPTY), T0, TZ, True)
    assert s["stale"] and s["devices"] == [] and s["nodes"] == []


def test_parse_netsh_keeps_no_mac_addresses():
    nets = wifi.parse_netsh(NETSH)
    assert [(n["ssid"], n["channel"], n["band"], n["dbm"]) for n in nets] == [
        ("Neighbor One", 6, "2.4 GHz", -60), ("Neighbor Two", 6, "2.4 GHz", -80), ("Neighbor Two", 44, "5 GHz", -70), ("(hidden)", 5, "2.4 GHz", -75)]
    assert not re.search(MAC, json.dumps(nets))
    with pytest.raises(ValueError):
        wifi.parse_netsh("nothing useful here")
    with pytest.raises(ValueError):
        wifi.clean_scan("nope")


def radios(ch):
    return [{"name": "N", "radios": {"2.4 GHz": {"channel": ch}, "5 GHz": {"channel": 44}}}]


def test_interference_suggests_a_quieter_channel_only_when_clearly_better():
    crowded = [{"ssid": f"n{i}", "channel": 6, "band": "2.4 GHz", "dbm": -60} for i in range(4)] + [{"ssid": "x", "channel": 44, "band": "5 GHz", "dbm": -60}]
    r = wifi.interference(radios(6), crowded)
    assert r["scanned"] and r["ours_24"] == [6] and r["best_24"] in (1, 11) and "cannot change" in r["suggestion"] and r["same_5"] == {"44": 1}
    assert wifi.interference(radios(1), crowded)["suggestion"] is None   # already quiet
    assert wifi.interference(radios(6), None) == {"scanned": False}


# ---------- routes ----------

@pytest.fixture
def cfg(settings, tmp_path):
    return replace(settings, collector_dir=tmp_path / "collector", media_dir=tmp_path / "media")


def login(cfg, admin=True, app=None):
    app = app or create_app(cfg, builder=FakeBuilder(), collect=False)
    name = "alex" if admin else "guest"
    make_user(app, name)
    with app.state.SessionLocal() as db:
        db.query(User).filter(User.username == name).update({"is_admin": admin})
        db.commit()
    c = TestClient(app)
    sign_in(c, name)
    return app, c


def seed(cfg):
    cfg.collector_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    write_eero(cfg.collector_dir, now, [client(PHONE), client(TV, "-80 dBm", band="2.4 GHz")])
    st = wifi.WifiTracker(cfg.collector_dir).tick()
    (cfg.collector_dir / "wifi.json").write_text(json.dumps({"checked_at": presence.iso(now), "ok": True, "data": st}))


def test_api_requires_sign_in(cfg):
    c = TestClient(create_app(cfg, builder=FakeBuilder(), collect=False))
    assert c.get("/api/wifi", follow_redirects=False).status_code in (401, 302, 303, 307)
    assert c.put("/api/wifi/places", json={}, headers=OK, follow_redirects=False).status_code in (401, 302, 303, 307)


def test_api_shapes_for_admin_and_member_and_no_mac(cfg):
    seed(cfg)
    app, c = login(cfg, True)
    j = c.get("/api/wifi").json()
    assert {d["id"] for d in j["devices"]} == {PHONE, TV} and j["places"] == {} and j["interference"] == {"scanned": False} and j["scan"] is None
    assert not re.search(MAC, json.dumps(j))
    _, m = login(cfg, False, app)
    j = m.get("/api/wifi").json()
    assert "devices" not in j and "places" not in j and j["nodes"][0]["name"] == "Living room"


def test_api_works_with_no_history_file(cfg):
    _, c = login(cfg)
    j = c.get("/api/wifi").json()
    assert j["stale"] and j["devices"] == [] and j["nodes"] == []


def test_node_positions_come_from_the_floorplan_layout(cfg):
    seed(cfg)
    app, c = login(cfg)
    from app import settingsstore
    with app.state.SessionLocal() as db:
        settingsstore.put(db, "floorplan_layout", json.dumps({"items": [{"id": "eero:living-room", "kind": "eero", "x": .4, "y": .6}], "links": []}))
    n = c.get("/api/wifi").json()["nodes"][0]
    assert (n["x"], n["y"]) == (.4, .6)


def test_places_validation_origin_and_admin_only(cfg):
    seed(cfg)
    app, c = login(cfg)
    assert c.put("/api/wifi/places", json={"places": {PHONE: {"x": .2, "y": .4}}}, headers=BAD).status_code == 403   # cross-site
    assert c.put("/api/wifi/places", json={"places": {"not-an-id": {"x": .2, "y": .3}}}, headers=OK).status_code == 422
    assert c.put("/api/wifi/places", json={"places": {PHONE: {"x": "a", "y": .3}}}, headers=OK).status_code == 422
    assert c.put("/api/wifi/places", json=[1], headers=OK).status_code == 422
    r = c.put("/api/wifi/places", json={"places": {PHONE: {"x": .2, "y": 1.4}}}, headers=OK)
    assert r.status_code == 200 and r.json()["places"][PHONE] == {"x": .2, "y": 1.0}   # clamped to the drawing
    assert c.get("/api/wifi").json()["places"][PHONE]["x"] == .2
    _, m = login(cfg, False, app)
    assert m.put("/api/wifi/places", json={"places": {}}, headers=OK).status_code == 403


def test_scan_upload_and_clear(cfg):
    seed(cfg)
    _, c = login(cfg)
    assert c.post("/api/wifi/scan", json={"text": NETSH}, headers=BAD).status_code == 403
    assert c.post("/api/wifi/scan", json={"text": "junk"}, headers=OK).status_code == 422
    assert c.post("/api/wifi/scan", json=["x"], headers=OK).status_code == 422
    assert c.post("/api/wifi/scan", json={"text": NETSH}, headers=OK).json() == {"count": 4}
    j = c.get("/api/wifi").json()
    assert j["scan"]["count"] == 4 and j["interference"]["scanned"] and j["interference"]["ours_24"] == [6]
    assert not re.search(MAC, json.dumps(j))
    assert c.post("/api/wifi/scan", json={"networks": [{"ssid": "A", "channel": 11, "dbm": -50}]}, headers=OK).json() == {"count": 1}
    assert c.delete("/api/wifi/scan", headers=OK).status_code == 200
    assert c.get("/api/wifi").json()["scan"] is None


def test_page_and_menu(cfg):
    _, c = login(cfg)
    r = c.get("/wifi")
    assert r.status_code == 200 and "/static/wifi.js" in r.text and "No floorplan yet" in r.text and 'href="/wifi"' in r.text
    cfg.media_dir.mkdir()
    (cfg.media_dir / "floorplan.jpg").write_bytes(b"x")
    assert "/floorplan/image" in c.get("/wifi").text


def test_wifi_files_hold_nothing_private():
    root = Path(__file__).resolve().parents[2]
    for rel in ("portal/app/wifi.py", "portal/app/routes_wifi.py", "portal/app/static/wifi.js", "portal/app/templates/wifi.html"):
        text = (root / rel).read_text(encoding="utf-8")
        assert not re.search(MAC + "|Kingsley|Cambridge", text), rel


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_wifi_js_parses():
    assert subprocess.run(["node", "--check", str(STATIC / "wifi.js")], capture_output=True).returncode == 0
