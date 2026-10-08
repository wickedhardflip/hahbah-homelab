"""Slice 5: deploy history, the what-changed footer, the Edge & domain panel's numbers."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.collectors.deploy import last_deploy
from app.main import create_app
from app.views import edge_panel, snapshot_for, whatchanged
from conftest import FakeBuilder, make_user, sign_in

NOW = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)
TZ = "America/New_York"
OK = "2026-10-07T10:00:00-04:00 deployed {c} ({m})\n"


def test_history_is_last_ten_valid_lines_newest_first_and_skips_bad_ones(tmp_path):
    log = tmp_path / "deploy.log"
    lines = [OK.format(c=f"abc{i:04d}", m=f"change {i}") for i in range(12)]
    lines.insert(5, "garbage line\n")
    log.write_text("".join(lines))
    d = last_deploy(log, history=True)
    assert [x["commit"] for x in d["history"]] == [f"abc{i:04d}" for i in range(11, 1, -1)]
    assert d["commit"] == "abc0011" and d["ok"] is True


def test_last_deploy_without_history_is_unchanged(tmp_path):
    log = tmp_path / "deploy.log"
    log.write_text(OK.format(c="abc1234", m="x"))
    assert "history" not in last_deploy(log)


def test_history_marks_failures(tmp_path):
    log = tmp_path / "deploy.log"
    log.write_text(OK.format(c="aaa1111", m="a") + "2026-10-07T11:00:00-04:00 FAILED build at bbb2222 (b)\n")
    assert [x["ok"] for x in last_deploy(log, history=True)["history"]] == [False, True]


def test_snapshot_carries_history():
    from test_snapshot import build
    hist = [{"commit": "bbb2222", "message": "b", "at": "x", "ok": True}]
    d = {"commit": "bbb2222", "message": "b", "at": "x", "ok": True, "history": hist}
    assert build(deploy=d)["edge"]["deploy"]["history"] == hist


def snap(history, alerts=()):
    return {"edge": {"deploy": {"commit": history[0]["commit"], "message": history[0]["message"], "at": history[0]["at"],
                                "ok": history[0]["ok"], "history": history}}, "alerts": list(alerts), "nodes": [], "links": []}


def dep(c, m, ok=True, at="2026-10-07T10:00:00-04:00"):
    return {"commit": c, "message": m, "at": at, "ok": ok}


def inc(msg, opened, sev="warn", target="nas", kind="disk_high", closed=None):
    return {"key": f"{kind}:{target}", "kind": kind, "target": target, "severity": sev, "message": msg,
            "opened_at": opened, "closed_at": closed}


def test_footer_summary_and_today_count():
    s = snap([dep("a1b2c3d", "tune caddy", at="2026-10-07T10:00:00-04:00")])
    incs = [inc("NAS full", NOW - timedelta(hours=1)), inc("Old", NOW - timedelta(days=2))]
    w = whatchanged(s, incs, NOW, TZ, show_subjects=True)
    assert w["deployed"]["commit"] == "a1b2c3d" and w["deployed"]["ago"] == "2 h ago" and w["today"] == 1
    assert [i["message"] for i in w["incidents"]] == ["NAS full", "Old"]


def test_footer_with_nothing_recorded():
    w = whatchanged({"edge": {}}, [], NOW, TZ, show_subjects=True)
    assert w["deployed"] is None and w["today"] == 0 and w["deploys"] == []


def test_failed_deploy_flagged_and_subjects_hidden_for_non_admins():
    s = snap([dep("bbb2222", "jobs: tweak scraper", ok=False)])
    w = whatchanged(snapshot_for(s, False), [inc("The last deploy failed: jobs: tweak scraper", NOW, kind="deploy_failed", target="caddy")],
                    NOW, TZ, show_subjects=False)
    assert w["deploys"][0]["ok"] is False
    assert "tweak scraper" not in json.dumps(w)


def test_discreet_drops_job_scraper_incidents_and_subjects():
    s = snap([dep("bbb2222", "jobs: tweak scraper")])
    incs = [inc("Example App is down", NOW, sev="crit", target="jobs", kind="app_down"), inc("NAS full", NOW)]
    w = whatchanged(snapshot_for(s, True, True), incs, NOW, TZ, show_subjects=False, discreet_mode=True)
    blob = json.dumps(w).lower()
    assert "example app" not in blob and "scraper" not in blob and w["today"] == 1


def test_snapshot_for_blanks_history_messages_for_non_admin_and_discreet():
    s = snap([dep("a1b2c3d", "secret subject")])
    for admin, mode in ((False, False), (True, True)):
        assert "secret subject" not in json.dumps(snapshot_for(s, admin, mode))
    assert "secret subject" in json.dumps(snapshot_for(s, True, False))


# ---------- Edge & domain numbers ----------
EDGE = {"cert": {"expires": "2026-10-27T16:00:00Z", "issuer": "Let's Encrypt (R11)", "names": ["*.hahbah.com"]},
        "domain": {"expires": "2027-09-30T00:00:00Z", "registrar": "Porkbun", "auto_renew": True}}


def test_edge_panel_tones_at_thresholds():
    def tone(days):
        e = {"cert": {**EDGE["cert"], "expires": (NOW + timedelta(days=days, minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
        return edge_panel(e, NOW)["cert"]["tone"]
    assert [tone(d) for d in (30, 21, 20, 7, 6, 0)] == ["", "", "warn", "warn", "crit", "crit"]


def test_edge_panel_values_and_missing_data():
    p = edge_panel(EDGE, NOW)
    assert p["cert"]["days"] == 20 and p["cert"]["issuer"] == "Let's Encrypt (R11)"
    assert p["domain"]["registrar"] == "Porkbun" and p["domain"]["tone"] == "" and p["domain"]["days"] > 300
    assert edge_panel({}, NOW) == {"cert": None, "domain": None}
    assert edge_panel({"cert": {"error": "x"}, "domain": {"expires": "junk"}}, NOW) == {"cert": None, "domain": None}


# ---------- page ----------
def page_app(settings, tmp_path, snapshot):
    ob = tmp_path / "outbox"
    ob.mkdir()
    return create_app(replace(settings, outbox_dir=ob), builder=FakeBuilder(snapshot), collect=False)


def login(app, admin):
    make_user(app)
    from app.models import User
    with app.state.SessionLocal() as db:
        db.query(User).one().is_admin = admin
        db.commit()
    c = TestClient(app)
    sign_in(c)
    return c


def test_dashboard_footer_renders_and_is_scrubbed(settings, tmp_path):
    s = {**snap([dep("a1b2c3d", "jobs: tweak scraper"), dep("0ldc0de", "older")]), "layout": {"lines": [], "pos": {}, "via": {}}}
    c = login(page_app(settings, tmp_path, s), True)
    html = c.get("/").text
    assert 'id="whatchanged"' in html and "a1b2c3d" in html and "tweak scraper" in html
    c.post("/settings", data={"action": "save", "discreet": "1"}, headers={"Origin": "https://home.hahbah.com"})
    html = c.get("/").text
    assert "a1b2c3d" in html and "tweak scraper" not in html


def test_dashboard_without_deploys_still_renders_footer(settings, tmp_path):
    c = login(page_app(settings, tmp_path, {"nodes": [], "links": [], "alerts": [], "edge": {},
                                            "layout": {"lines": [], "pos": {}, "via": {}}}), False)
    assert 'id="whatchanged"' in c.get("/").text
