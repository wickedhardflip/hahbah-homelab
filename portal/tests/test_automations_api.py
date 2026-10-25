"""Presence + automations API and pages: auth, Origin, validation, caps, shapes. Collector files written by hand; no network."""
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import presence
from app.automations import plug_key
from app.main import create_app
from app.models import User
from conftest import FakeBuilder, make_user, sign_in

STATIC = Path(__file__).parent.parent / "app" / "static"
OK = {"origin": "https://home.example.com"}
HALL = "AAAA00000001"
HK = plug_key(HALL)
ALEX_PHONE, TV = "a1a1a1a1a1", "c3c3c3c3c3"
PEOPLE = {"people": [{"id": "alex", "name": "Alex", "devices": [ALEX_PHONE]}], "away_after_min": 10}
RULE = {"id": "r1", "name": "Away lamp", "enabled": True, "when": [{"type": "house", "who": "nobody", "for_min": 0}],
        "then": {"do": "on_until", "plug": HK, "until": "23:00"}}


@pytest.fixture
def cfg(settings, tmp_path):
    return replace(settings, plugs_dir=tmp_path / "plugs", collector_dir=tmp_path / "collector")


def files(cfg, stale=False):
    """wyze.json + automations.json the way the collector writes them."""
    cfg.collector_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    env = lambda data, age=0: {"checked_at": presence.iso(now - timedelta(seconds=age)), "interval_s": 30, "ok": True, "data": data}  # noqa: E731
    (cfg.collector_dir / "wyze.json").write_text(json.dumps(env({"plugs": [
        {"id": HALL, "model": "WLPP1CFH", "nickname": "Hall lamp", "online": True, "on": False, "away": False}]})))
    st = {**presence.EMPTY, "stale": False, "eero_at": presence.iso(now),
          "people": [{"id": "alex", "name": "Alex", "devices": [ALEX_PHONE], "home": False, "since": presence.iso(now - timedelta(hours=1))}],
          "devices": {ALEX_PHONE: {"name": "Alex phone", "band": "5 GHz", "signal_dbm": "-50 dBm", "wired": False, "node": "Hall",
                                   "last_seen": presence.iso(now - timedelta(hours=1))},
                      TV: {"name": "Living room TV", "band": "5 GHz", "signal_dbm": "-60 dBm", "wired": False, "node": "Hall",
                           "last_seen": presence.iso(now)}},
          "history": [[presence.iso(now - timedelta(hours=1)), "alex", "leave"]],
          "rules": {"r1": {"sig": "x", "was": True, "stamp": None, "last": {"at": "t", "text": "Would turn Hall lamp on.", "ok": True}}},
          "pending": []}
    (cfg.collector_dir / "automations.json").write_text(json.dumps(env(st, age=3600 if stale else 0)))
    cfg.plugs_dir.mkdir(parents=True, exist_ok=True)
    (cfg.plugs_dir / "people.json").write_text(json.dumps(PEOPLE))


def mk(cfg, admin=True, signed_in=True):
    app = create_app(cfg, builder=FakeBuilder(), collect=False)
    make_user(app)
    with app.state.SessionLocal() as db:
        db.query(User).one().is_admin = admin
        db.commit()
    c = TestClient(app)
    if signed_in:
        sign_in(c)
    return c


WRITES = [("put", "/api/presence/people", PEOPLE), ("put", "/api/automations", {"rules": [RULE]}), ("post", "/api/automations/r1/test", {})]


def call(c, method, url, body, **kw):
    return getattr(c, method)(url, json=body, **kw)


def test_everything_needs_sign_in(cfg):
    c = mk(cfg, signed_in=False)
    for url in ("/api/presence", "/api/automations", "/api/automations/log"):
        assert c.get(url).status_code == 401
    for m, url, body in WRITES:
        assert call(c, m, url, body, headers=OK).status_code == 401
    assert c.get("/presence", follow_redirects=False).status_code == 303 and c.get("/automations", follow_redirects=False).status_code == 303


def test_non_admin_reads_but_cannot_write(cfg):
    files(cfg)
    c = mk(cfg, admin=False)
    p = c.get("/api/presence").json()
    assert p["people"][0]["name"] == "Alex" and "devices" not in p
    assert c.get("/api/automations").status_code == 200 and c.get("/api/automations/log").status_code == 200
    for m, url, body in WRITES:
        assert call(c, m, url, body, headers=OK).status_code == 403
    assert not (cfg.plugs_dir / "rules.json").exists()


def test_cross_site_writes_are_refused(cfg):
    files(cfg)
    c = mk(cfg)
    for m, url, body in WRITES:
        assert call(c, m, url, body, headers={"origin": "https://evil.example"}).status_code == 403
    assert not (cfg.plugs_dir / "rules.json").exists()


def test_presence_for_admins_lists_devices_by_hash(cfg):
    files(cfg)
    p = mk(cfg).get("/api/presence").json()
    assert p["configured"] and not p["stale"] and p["history"][0]["name"] == "Alex" and p["history"][0]["event"] == "leave"
    rows = {d["id"]: d for d in p["devices"]}
    assert rows[ALEX_PHONE]["person"] == "alex" and rows[TV]["connected"] and not rows[ALEX_PHONE]["connected"]


def test_old_collector_state_reads_as_stale(cfg):
    files(cfg, stale=True)
    assert mk(cfg).get("/api/presence").json()["stale"] is True


def test_save_people_and_validation(cfg):
    files(cfg)
    c = mk(cfg)
    new = {"people": [{"id": "alex", "name": "Alex", "devices": [ALEX_PHONE, TV]}], "away_after_min": 15}
    assert c.put("/api/presence/people", json=new, headers=OK).status_code == 200
    assert json.loads((cfg.plugs_dir / "people.json").read_text()) == new
    for bad, msg in [({"people": "x"}, "Send people"), ({"people": [{"id": "a", "name": "A", "devices": ["AA:BB:CC:DD:EE:FF"]}]}, "short ids"),
                     ({"people": [], "away_after_min": 500}, "Away after"), ([], "JSON object")]:
        r = c.put("/api/presence/people", json=bad, headers=OK)
        assert r.status_code == 422 and msg in r.json()["detail"], bad


def test_rules_round_trip_default_to_dry_run(cfg):
    files(cfg)
    c = mk(cfg)
    r = c.put("/api/automations", json={"rules": [RULE]}, headers=OK)
    assert r.status_code == 200 and r.json()["rules"][0]["mode"] == "dry_run"
    got = c.get("/api/automations").json()
    assert got["rules"][0]["id"] == "r1" and got["status"]["r1"]["last"]["text"].startswith("Would")
    assert got["plugs"] == [{"key": HK, "name": "Hall lamp", "online": True, "on": False}] and got["people"] == [{"id": "alex", "name": "Alex"}]
    assert got["sun"]["sunset"].endswith("PM")
    assert HALL not in json.dumps(got)   # the Wyze id (a MAC) never shows here


@pytest.mark.parametrize("body, msg", [
    ({"rules": [{**RULE, "then": {"do": "on", "plug": "0123456789"}}]}, "not in the live data"),
    ({"rules": [{**RULE, "when": [{"type": "people", "people": ["zed"], "state": "home"}]}]}, "Presence page"),
    ({"rules": [RULE, RULE]}, "unique"),
    ({"rules": [{**RULE, "id": f"r{i}"} for i in range(31)]}, "At most 30"),
    ({"rules": [{**RULE, "name": "<b>" + "x" * 40}]}, "1 to 30"),
    ({"nope": 1}, "Send rules"),
])
def test_bad_rules_get_a_plain_422(cfg, body, msg):
    files(cfg)
    r = mk(cfg).put("/api/automations", json=body, headers=OK)
    assert r.status_code == 422 and msg in r.json()["detail"]
    assert not (cfg.plugs_dir / "rules.json").exists()


def test_test_now_explains_and_changes_nothing(cfg):
    files(cfg)
    c = mk(cfg)
    assert c.post("/api/automations/r1/test", json={}, headers=OK).status_code == 404
    c.put("/api/automations", json={"rules": [RULE]}, headers=OK)
    before = sorted(p.name for p in cfg.plugs_dir.rglob("*"))
    t = c.post("/api/automations/r1/test", json={}, headers=OK).json()
    assert t["conditions"] == [{"text": "Nobody is home", "ok": True}] and t["all"] is True and t["mode"] == "dry_run"
    assert t["would"] in ("Turn Hall lamp on until 11:00 PM, then off.", "Nothing: it is already past 11:00 PM.")
    unsaved = {**RULE, "when": [{"type": "house", "who": "anyone", "for_min": 0}]}
    t = c.post("/api/automations/r1/test", json={"rule": unsaved}, headers=OK).json()
    assert t["all"] is False and t["would"].startswith("Nothing")
    assert c.post("/api/automations/r1/test", json={"rule": {"id": "x"}}, headers=OK).status_code == 422
    assert sorted(p.name for p in cfg.plugs_dir.rglob("*")) == before


def test_log_newest_first_and_capped(cfg):
    files(cfg)
    lines = [json.dumps({"at": f"t{i}", "rule": "r1", "name": "Away lamp", "mode": "live", "text": f"line {i}", "ok": True}) for i in range(400)]
    (cfg.plugs_dir / "activity.jsonl").write_text("\n".join(lines) + "\n")
    c = mk(cfg)
    assert c.get("/api/automations/log").json()["entries"][0]["text"] == "line 399"
    assert len(c.get("/api/automations/log?limit=5000").json()["entries"]) == 300


def test_not_set_up_without_a_plugs_folder(settings):
    c = mk(replace(settings, plugs_dir=None))
    assert c.get("/api/presence").json()["configured"] is False and c.get("/api/automations").json()["rules"] == []
    assert c.put("/api/automations", json={"rules": []}, headers=OK).status_code == 404


def test_pages_render_and_link_from_the_menu(cfg):
    files(cfg)
    c = mk(cfg)
    for url, js in (("/presence", "presence.js"), ("/automations", "automations.js")):
        r = c.get(url)
        assert r.status_code == 200 and js in r.text and 'href="/presence"' in r.text and 'href="/automations"' in r.text
    assert all(".innerHTML" not in (STATIC / n).read_text(encoding="utf-8") for n in ("presence.js", "automations.js", "whoshome.js"))


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("name", ["presence.js", "automations.js", "whoshome.js", "plugs.js"])
def test_js_parses(name):
    assert subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True).returncode == 0
