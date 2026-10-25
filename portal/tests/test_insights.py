from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import insights, settingsstore
from app.main import create_app
from app.models import DailyMetric, Incident, User
from conftest import FakeBuilder, make_user, sign_in

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)   # a Monday
TODAY = date(2026, 10, 26)


def line(start, step, n):
    return [(TODAY - timedelta(days=n - 1 - i), start + step * i) for i in range(n)]


def test_forecast_filling_flat_and_short():
    f = insights.forecast(line(50.0, 0.5, 20))   # 59.5% now, +0.5/day -> 81 days to 100
    assert f["state"] == "filling" and f["full_in_days"] == 81 and f["full_on"] == (TODAY + timedelta(days=81)).isoformat()
    assert insights.forecast(line(60.0, 0.0, 20))["state"] == "flat"
    assert insights.forecast(line(60.0, -0.3, 20))["state"] == "flat"
    assert insights.forecast(line(60.0, 1.0, 3)) == {"state": "short", "points": 3}


def test_grade_bands():
    assert insights.grade(1.0, 0, 0) == "A+"
    assert insights.grade(0.82, 0, 0) == "B-"
    assert insights.grade(0.95, 100, 1) == "C"   # 95 - 20 - 2 = 73
    assert insights.grade(0.2, 0, 0) == "F"
    assert insights.grade(None, 0, 0) == "-"


@pytest.fixture
def app(settings, tmp_path):
    out = tmp_path / "outbox"
    a = create_app(replace(settings, outbox_dir=out, alert_to="me@example.com"), builder=FakeBuilder(), collect=False)
    make_user(a)
    with a.state.SessionLocal() as db:
        db.query(User).update({"is_admin": True})
        for day, v in line(400.0, 0.0, 10):
            db.add(DailyMetric(day=day.isoformat(), key="speed_down", value=v))
            db.add(DailyMetric(day=day.isoformat(), key="speed_up", value=20.0))
        for day, v in line(50.0, 1.0, 10):
            db.add(DailyMetric(day=day.isoformat(), key="nas_used_pct", value=v))
        opened = NOW.replace(tzinfo=None) - timedelta(days=3)
        db.add(Incident(key="host_down:router", kind="host_down", target="router", severity="crit", message="The Eero stopped answering ping.",
                        opened_at=opened, closed_at=opened + timedelta(minutes=41)))
        db.add(Incident(key="disk_high:nas", kind="disk_high", target="nas", severity="warn", message="not counted", opened_at=opened))
        db.commit()
    return a


def test_isp_report_reads_history(app):
    with app.state.SessionLocal() as db:
        r = insights.report(db, NOW, "America/New_York")
    isp = r["isp"]
    assert isp["held_pct"] == 80 and isp["avg_down"] == 400.0 and isp["outage_minutes"] == 41 and len(isp["outages"]) == 1
    assert isp["grade"] == "C-"   # 80 - 41/5 = 71.8
    assert r["forecast"]["state"] == "filling" and len(r["forecast"]["series"]) == 10


def test_pages_and_digest_off_by_default(app, tmp_path):
    c = TestClient(app)
    assert TestClient(app, follow_redirects=False).get("/api/insights").status_code in (302, 303, 401)
    sign_in(c)
    assert c.get("/insights").status_code == 200
    assert c.get("/api/insights").json()["isp"]["plan"] == {"down": 500, "up": 20}
    html = c.get("/insights/digest-preview").text
    assert "Your week at HAHBAH" in html and "NAS disk" in html
    assert not list((tmp_path / "outbox").glob("*.eml"))   # a preview sends nothing


def test_weekly_tick_sends_only_when_on_and_once(app, tmp_path):
    rec, c = app.state.recorder, TestClient(app)
    sign_in(c)
    rec.now = lambda: NOW
    outbox = tmp_path / "outbox"
    with app.state.SessionLocal() as db:
        settingsstore.put(db, "digest_enabled", "0")   # keep the morning report out of this count
    app.state.recorder.observe({"alerts": [], "nodes": []})
    assert not list(outbox.glob("*.eml"))                       # off by default
    assert c.post("/api/insights/digest", json={"enabled": True}, headers={"Origin": "https://home.example.com"}).json() == {"enabled": True}
    app.state.recorder.observe({"alerts": [], "nodes": []})
    app.state.recorder.observe({"alerts": [], "nodes": []})
    assert len(list(outbox.glob("digest-*.eml"))) == 1          # once per week


def test_toggle_needs_same_origin(app):
    c = TestClient(app)
    sign_in(c)
    assert c.post("/api/insights/digest", json={"enabled": True}, headers={"Origin": "https://evil.example"}).status_code == 403
