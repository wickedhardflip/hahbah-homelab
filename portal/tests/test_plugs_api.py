"""Smart plug + floorplan layout API. wyze.json is written by hand the way the collector writes it; no network."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import User
from conftest import FakeBuilder, make_user, sign_in

OK = {"origin": "https://home.example.com"}
SNAP = {"schema": "homelab.snapshot/v1", "alerts": [], "links": [], "layout": {"lines": [], "pos": {}, "via": {}}, "edge": {}, "sources": [],
        "nodes": [{"id": "central", "kind": "host", "label": "Central", "sublabel": "mini PC", "meta": {}},
                  {"id": "nas", "kind": "host", "label": "NAS", "meta": {}}, {"id": "plex", "kind": "app", "label": "Plex", "meta": {}}],
        "eero": {"live": {"nodes": [{"name": "Living Room", "model": "eero 6"}, {"name": None}],
                          "clients": [{"id": "abc123", "name": "Desktop", "wired": True, "ip": "192.168.1.9"},
                                      {"id": "def456", "name": "Phone", "wired": False}]}}}


def plug_row(pid="AA", nick="Entry Way", online=True, on=False):
    return {"id": pid, "model": "WLPP1CFH", "nickname": nick, "online": online, "on": on, "away": False,
            "last_cmd": {"action": "on", "by": "alex", "at": "2026-10-23T14:02:11Z", "ok": True}, "last_sched": None}


def write_wyze(settings, rows, age_s=0, ok=True, error=None):
    settings.collector_dir.mkdir(parents=True, exist_ok=True)
    at = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {"plugs": rows, **({"error": error} if error else {})}
    (settings.collector_dir / "wyze.json").write_text(json.dumps({"checked_at": at, "interval_s": 300, "ok": ok, "data": data}))


@pytest.fixture
def cfg(settings, tmp_path):
    return replace(settings, plugs_dir=tmp_path / "plugs", collector_dir=tmp_path / "collector")


def mk(cfg, admin=True, signed_in=True):
    app = create_app(cfg, builder=FakeBuilder(SNAP), collect=False)
    make_user(app)
    with app.state.SessionLocal() as db:
        db.query(User).one().is_admin = admin
        db.commit()
    c = TestClient(app)
    if signed_in:
        sign_in(c)
    return c


def cmds(cfg):
    d = cfg.plugs_dir / "cmd"
    return [json.loads(f.read_text()) for f in sorted(d.glob("*.json"))] if d.exists() else []


GOOD = {"label": "Hallway lamp", "schedules_enabled": True,
        "schedules": [{"id": "s1", "days": [0, 1, 2, 3, 4], "time": "17:30", "action": "on", "enabled": True}]}


def test_everything_needs_sign_in(cfg):
    c = mk(cfg, signed_in=False)
    write_wyze(cfg, [plug_row()])
    for call in (c.get("/api/plugs"), c.get("/api/floorplan"), c.post("/api/plugs/AA/power", json={"on": True}),
                 c.post("/api/plugs/AA/away", json={"on": True}), c.put("/api/plugs/AA/config", json=GOOD), c.put("/api/floorplan", json={})):
        assert call.status_code == 401
    assert cmds(cfg) == []


def test_non_admin_reads_but_cannot_write(cfg):
    c = mk(cfg, admin=False)
    write_wyze(cfg, [plug_row()])
    assert c.get("/api/plugs").status_code == 200 and c.get("/api/floorplan").status_code == 200
    for call in (c.post("/api/plugs/AA/power", json={"on": True}), c.post("/api/plugs/AA/away", json={"on": True}),
                 c.put("/api/plugs/AA/config", json=GOOD), c.put("/api/floorplan", json={"items": [], "links": []})):
        assert call.status_code == 403
    assert cmds(cfg) == [] and not (cfg.plugs_dir / "config.json").exists()


def test_cross_site_writes_are_refused(cfg):
    c = mk(cfg)
    write_wyze(cfg, [plug_row()])
    bad = {"origin": "https://evil.example"}
    for call in (c.post("/api/plugs/AA/power", json={"on": True}, headers=bad), c.post("/api/plugs/AA/away", json={"on": True}, headers=bad),
                 c.put("/api/plugs/AA/config", json=GOOD, headers=bad), c.put("/api/floorplan", json={"items": [], "links": []}, headers=bad)):
        assert call.status_code == 403
    assert cmds(cfg) == []


def test_not_configured_when_there_is_no_wyze_file(cfg):
    c = mk(cfg)
    r = c.get("/api/plugs").json()
    assert r == {"plugs": [], "checked_at": None, "stale": False, "configured": False}
    assert c.post("/api/plugs/AA/power", json={"on": True}, headers=OK).status_code == 404


def test_plugs_list_merges_settings_and_flags(cfg):
    c = mk(cfg)
    write_wyze(cfg, [plug_row("AA"), plug_row("BB", "Garage", online=False)])
    assert c.put("/api/plugs/AA/config", json=GOOD, headers=OK).status_code == 200
    r = c.get("/api/plugs").json()
    assert r["configured"] is True and r["stale"] is False and r["checked_at"]
    a, b = r["plugs"]
    assert a["name"] == "Hallway lamp" and a["label"] == "Hallway lamp" and a["nickname"] == "Entry Way" and a["schedules_enabled"] is True
    assert a["schedules"] == GOOD["schedules"] and a["next"] and a["next"].endswith("17:30 on") and a["last_cmd"]["by"] == "alex"
    assert b["name"] == "Garage" and b["online"] is False and b["schedules_enabled"] is False and b["schedules"] == [] and b["next"] is None
    assert set(a) == {"id", "name", "nickname", "label", "model", "online", "on", "away", "schedules_enabled", "schedules",
                      "last_cmd", "last_sched", "next"}


@pytest.mark.parametrize("kw", [{"age_s": 901}, {"ok": False}, {"error": "RuntimeError"}])
def test_stale_flag(cfg, kw):
    c = mk(cfg)
    write_wyze(cfg, [plug_row()], **kw)
    r = c.get("/api/plugs").json()
    assert r["stale"] is True and r["configured"] is True and len(r["plugs"]) == 1


def test_unreadable_wyze_file_means_not_configured(cfg):
    c = mk(cfg)
    cfg.collector_dir.mkdir(parents=True)
    (cfg.collector_dir / "wyze.json").write_text("{nope")
    assert c.get("/api/plugs").json()["configured"] is False


@pytest.mark.parametrize("route,action_on,action_off", [("power", "on", "off"), ("away", "away_on", "away_off")])
def test_command_files_are_queued(cfg, route, action_on, action_off):
    c = mk(cfg)
    write_wyze(cfg, [plug_row()])
    r = c.post(f"/api/plugs/AA/{route}", json={"on": True}, headers=OK)
    assert r.status_code == 202 and r.json() == {"queued": True}
    c.app.state.plug_last.clear()
    assert c.post(f"/api/plugs/AA/{route}", json={"on": False}, headers=OK).status_code == 202
    assert sorted((x["id"], x["action"], x["by"]) for x in cmds(cfg)) == sorted([("AA", action_on, "alex"), ("AA", action_off, "alex")])
    assert not list((cfg.plugs_dir / "cmd").glob("*.tmp"))


def test_command_rate_limit_is_per_plug(cfg):
    c = mk(cfg)
    write_wyze(cfg, [plug_row("AA"), plug_row("BB")])
    assert c.post("/api/plugs/AA/power", json={"on": True}, headers=OK).status_code == 202
    assert c.post("/api/plugs/AA/power", json={"on": False}, headers=OK).status_code == 429
    assert c.post("/api/plugs/AA/away", json={"on": True}, headers=OK).status_code == 429
    assert c.post("/api/plugs/BB/power", json={"on": True}, headers=OK).status_code == 202
    assert len(cmds(cfg)) == 2


@pytest.mark.parametrize("body", [{"on": "yes"}, {"on": 1}, {}, [], "text", None])
def test_command_bodies_are_validated(cfg, body):
    c = mk(cfg)
    write_wyze(cfg, [plug_row()])
    assert c.post("/api/plugs/AA/power", json=body, headers=OK).status_code == 422
    assert c.post("/api/plugs/AA/power", content=b"{nope", headers={**OK, "content-type": "application/json"}).status_code == 422
    assert cmds(cfg) == []


def test_unknown_plug_ids_are_refused(cfg):
    c = mk(cfg)
    write_wyze(cfg, [plug_row()])
    for call in (c.post("/api/plugs/ZZ/power", json={"on": True}, headers=OK), c.post("/api/plugs/..%2Fx/away", json={"on": True}, headers=OK),
                 c.put("/api/plugs/ZZ/config", json=GOOD, headers=OK)):
        assert call.status_code == 404
    assert cmds(cfg) == [] and not (cfg.plugs_dir / "config.json").exists()


def test_writes_say_not_set_up_without_a_plugs_folder(settings, tmp_path):
    c = mk(replace(settings, collector_dir=tmp_path / "collector"))
    write_wyze(replace(settings, collector_dir=tmp_path / "collector"), [plug_row()])
    assert c.post("/api/plugs/AA/power", json={"on": True}, headers=OK).status_code == 404
    assert c.get("/api/plugs").json()["configured"] is True


@pytest.mark.parametrize("bad", [
    {**GOOD, "label": "x" * 41}, {**GOOD, "schedules": [{**GOOD["schedules"][0], "days": [9]}]},
    {**GOOD, "schedules": [{**GOOD["schedules"][0], "time": "25:00"}]}, {**GOOD, "schedules": [{**GOOD["schedules"][0], "action": "flip"}]},
    {**GOOD, "schedules": [{**GOOD["schedules"][0], "id": f"s{i}"} for i in range(21)]}, {"label": "a", "color": "red"}, [], "x"])
def test_config_put_gives_422_with_a_plain_message(cfg, bad):
    c = mk(cfg)
    write_wyze(cfg, [plug_row()])
    r = c.put("/api/plugs/AA/config", json=bad, headers=OK)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str) and r.json()["detail"]
    assert not (cfg.plugs_dir / "config.json").exists()


def test_config_put_replaces_one_plug_and_keeps_the_others(cfg):
    c = mk(cfg)
    write_wyze(cfg, [plug_row("AA"), plug_row("BB")])
    c.put("/api/plugs/AA/config", json=GOOD, headers=OK)
    c.put("/api/plugs/BB/config", json={"label": "Garage light"}, headers=OK)
    r = c.put("/api/plugs/AA/config", json={"label": "Lamp"}, headers=OK)   # a full replace: schedules are gone
    assert r.json() == {"id": "AA", "label": "Lamp", "schedules_enabled": False, "schedules": []}
    saved = json.loads((cfg.plugs_dir / "config.json").read_text())["plugs"]
    assert saved["AA"]["label"] == "Lamp" and saved["BB"]["label"] == "Garage light"


def test_nothing_returns_credentials(cfg, monkeypatch):
    monkeypatch.setenv("WYZE_PASSWORD", "pw-SECRET")
    monkeypatch.setenv("WYZE_API_KEY", "key-SECRET")
    c = mk(cfg)
    write_wyze(cfg, [plug_row()], error="RuntimeError")
    c.put("/api/plugs/AA/config", json=GOOD, headers=OK)
    shown = c.get("/api/plugs").text + c.get("/api/floorplan").text + (cfg.plugs_dir / "config.json").read_text()
    assert "SECRET" not in shown and "password" not in shown.lower() and "api_key" not in shown.lower()


# ---------- floorplan layout ----------

LAYOUT = {"items": [{"id": "plug:AA", "kind": "plug", "x": 0.42, "y": 0.18},
                    {"id": "eero:living-room", "kind": "eero", "x": 0.5, "y": 0.5},
                    {"id": "custom:ab12", "kind": "custom", "label": "Tree lights", "icon": "tree", "x": 0.3, "y": 0.2}],
          "links": [{"a": "plug:AA", "b": "custom:ab12", "kind": "power"}]}


def test_floorplan_get_lists_everything_placeable(cfg):
    c = mk(cfg)
    write_wyze(cfg, [plug_row("AA"), plug_row("BB", "Garage")])
    c.put("/api/plugs/AA/config", json={"label": "Hallway lamp"}, headers=OK)
    r = c.get("/api/floorplan").json()
    assert r["layout"] == {"items": [], "links": []} and r["image"] == "/floorplan/image"
    assert {a["id"]: (a["kind"], a["label"]) for a in r["available"]} == {
        "plug:AA": ("plug", "Hallway lamp"), "plug:BB": ("plug", "Garage"), "eero:living-room": ("eero", "Living Room"),
        "host:abc123": ("host", "Desktop"), "host:central": ("host", "Central"), "host:nas": ("host", "NAS")}
    assert all(set(a) == {"id", "kind", "label", "detail"} for a in r["available"])


def test_floorplan_put_roundtrip_and_clamping(cfg):
    c = mk(cfg)
    write_wyze(cfg, [plug_row()])
    odd = {**LAYOUT, "items": [{**LAYOUT["items"][0], "x": 1.7, "y": -3}] + LAYOUT["items"][1:]}
    r = c.put("/api/floorplan", json=odd, headers=OK)
    assert r.status_code == 200 and r.json()["layout"]["items"][0] == {"id": "plug:AA", "kind": "plug", "x": 1.0, "y": 0.0}
    assert c.get("/api/floorplan").json()["layout"] == r.json()["layout"]
    assert c.put("/api/floorplan", json=LAYOUT, headers=OK).json()["layout"] == LAYOUT


def test_floorplan_keeps_a_saved_plug_that_left_the_live_data(cfg):
    c = mk(cfg)
    write_wyze(cfg, [plug_row()])
    c.put("/api/floorplan", json=LAYOUT, headers=OK)
    write_wyze(cfg, [])   # Wyze returned nothing this time
    assert c.put("/api/floorplan", json=LAYOUT, headers=OK).status_code == 200
    assert len(c.get("/api/floorplan").json()["layout"]["items"]) == 3


def item(i, **kw):
    return {"id": f"custom:n{i}", "kind": "custom", "label": "L", "icon": "lamp", "x": 0.1, "y": 0.1, **kw}


@pytest.mark.parametrize("bad", [
    {"items": [{"id": "plug:ZZ", "kind": "plug", "x": 0, "y": 0}], "links": []},                   # not in live data
    {"items": [item(1, icon="rocket")], "links": []}, {"items": [item(1, label="")], "links": []},
    {"items": [item(1, label="x" * 41)], "links": []}, {"items": [item(1, x="left")], "links": []},
    {"items": [item(1, x=True)], "links": []}, {"items": [item(1, id="custom:BAD ID")], "links": []},
    {"items": [item(1), item(1)], "links": []}, {"items": [item(1, kind="plug")], "links": []},
    {"items": [item(i) for i in range(81)], "links": []},
    {"items": [item(1), item(2)], "links": [{"a": "custom:n1", "b": "custom:n2", "kind": "laser"}]},
    {"items": [item(1), item(2)], "links": [{"a": "custom:n1", "b": "custom:n9", "kind": "power"}]},
    {"items": [item(1)], "links": [{"a": "custom:n1", "b": "custom:n1", "kind": "power"}]},
    {"items": [item(1), item(2)], "links": [{"a": "custom:n1", "b": "custom:n2", "kind": "power"}] * 161},
    {"items": [], "links": [], "extra": 1}, [], "x"])
def test_floorplan_put_validation(cfg, bad):
    c = mk(cfg)
    write_wyze(cfg, [plug_row()])
    r = c.put("/api/floorplan", json=bad, headers=OK)
    assert r.status_code == 422 and r.json()["detail"]
    assert c.get("/api/floorplan").json()["layout"] == {"items": [], "links": []}
