from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Session as SessionRow, User
from conftest import FakeBuilder, make_user, sign_in

SNAP = {"schema": "homelab.snapshot/v1", "nodes": [{"id": "jobs", "kind": "app", "label": "Example App", "meta": {}},
                                                   {"id": "plex", "kind": "app", "label": "Plex", "meta": {}}],
        "links": [], "alerts": [], "layout": {"lines": [], "pos": {}, "via": {}}, "edge": {}, "sources": []}


@pytest.fixture
def sapp(settings, tmp_path):
    ob = tmp_path / "outbox"
    ob.mkdir()
    return create_app(replace(settings, outbox_dir=ob, alert_to="alex@example.com"), builder=FakeBuilder(SNAP), collect=False)


def client(app, admin=True):
    make_user(app)
    with app.state.SessionLocal() as db:
        db.query(User).one().is_admin = admin
        db.commit()
    c = TestClient(app)
    sign_in(c)
    return c


def test_settings_is_admin_only(sapp):
    assert client(sapp, admin=False).get("/settings").status_code == 403


def test_discreet_mode_hides_jobs_from_the_api(sapp):
    c = client(sapp)
    page = c.get("/settings").text
    assert "Discreet mode" in page and "Example App" not in page.split("<main>")[1].split("Accounts")[0]
    assert c.post("/settings", data={"action": "save", "discreet": "1", "digest_enabled": "1", "alerts_enabled": "1",
                                     "digest_time": "07:00"}, follow_redirects=False).status_code == 303
    ids = [n["id"] for n in c.get("/api/snapshot").json()["nodes"]]
    assert ids == ["plex"]
    assert "Example App" not in c.get("/").text


def test_cross_site_posts_are_refused(sapp):
    c = client(sapp)
    r = c.post("/settings", data={"action": "save", "discreet": "1"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_mute_test_email_and_sign_out_everywhere(sapp, tmp_path):
    c = client(sapp)
    c.post("/settings", data={"action": "mute", "hours": "4"})
    assert "Muted until" in c.get("/settings").text
    c.post("/settings", data={"action": "test_email"})
    assert len(list((tmp_path / "outbox").glob("test-*.eml"))) == 1
    with sapp.state.SessionLocal() as db:
        uid = db.query(User).one().id
    c.post("/settings", data={"action": "signout_user", "user_id": str(uid)}, follow_redirects=False)
    with sapp.state.SessionLocal() as db:
        assert db.query(SessionRow).count() == 0


def test_bad_digest_time_is_ignored(sapp):
    c = client(sapp)
    c.post("/settings", data={"action": "save", "digest_time": "03:17", "digest_enabled": "1", "alerts_enabled": "1"})
    assert 'value="06:30" selected' in c.get("/settings").text


def test_recipient_switches_only_touch_allowlisted_addresses(settings, tmp_path):
    ob = tmp_path / "outbox"
    ob.mkdir()
    app = create_app(replace(settings, outbox_dir=ob, alert_to="alex@example.com,wife@example.com"),
                     builder=FakeBuilder(SNAP), collect=False)
    c = client(app)
    page = c.get("/settings").text
    assert "wife@example.com" in page and "primary" in page
    c.post("/settings", data={"action": "save", "digest_enabled": "1", "alerts_enabled": "1", "digest_time": "06:30",
                              "recipients_form": "1", "alert_to": ["alex@example.com", "stranger@evil.example"],
                              "report_to": ["wife@example.com"]})
    from app import settingsstore
    with app.state.SessionLocal() as db:
        assert settingsstore.get(db, "alerts_off") == "wife@example.com"
        assert settingsstore.get(db, "report_off") == "alex@example.com"
    assert "stranger" not in c.get("/settings").text


def test_idle_timeout_is_saved_validated_and_shown(sapp):
    from app import settingsstore
    c = client(sapp)
    assert 'name="idle_minutes" value="480"' in c.get("/settings").text
    for bad in ("14", "10081", "abc", "1.5", "-5"):
        r = c.post("/settings", data={"action": "save", "idle_minutes": bad}, follow_redirects=False)
        assert r.headers["location"].endswith("idle-invalid"), bad
    with sapp.state.SessionLocal() as db:
        assert settingsstore.idle_minutes(db) == 480
    c.post("/settings", data={"action": "save", "idle_minutes": "90"})
    with sapp.state.SessionLocal() as db:
        assert settingsstore.idle_minutes(db) == 90
    assert 'name="idle_minutes" value="90"' in c.get("/settings").text
    assert "idle-invalid" not in c.post("/settings", data={"action": "save"}, follow_redirects=False).headers["location"]
    with sapp.state.SessionLocal() as db:   # a form without the field leaves it alone
        assert settingsstore.idle_minutes(db) == 90
