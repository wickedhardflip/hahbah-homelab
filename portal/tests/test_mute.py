from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import settingsstore
from conftest import make_user, sign_in

ISO = "%Y-%m-%dT%H:%M:%SZ"


def signed_in(app, name="alex"):
    make_user(app, name)
    c = TestClient(app)
    sign_in(c, name)
    return c


def stored(app, key):
    with app.state.SessionLocal() as db:
        return settingsstore.get(db, key)


def put(app, key, val):
    with app.state.SessionLocal() as db:
        settingsstore.put(db, key, val)


def test_mute_needs_sign_in(client):
    assert client.post("/api/mute", json={"hours": 1}).status_code == 401


def test_any_user_can_mute_and_unmute_and_mute_by_is_recorded(app):
    c = signed_in(app)   # not an admin
    r = c.post("/api/mute", json={"hours": 4}, headers={"Origin": "https://home.hahbah.com"})
    assert r.status_code == 200 and r.json()["muted"] and r.json()["by"] == "alex"
    until = datetime.strptime(stored(app, "mute_until"), ISO).replace(tzinfo=timezone.utc)
    assert timedelta(hours=3, minutes=59) < until - datetime.now(timezone.utc) <= timedelta(hours=4)
    assert stored(app, "mute_by") == "alex"
    r = c.post("/api/mute", json={"hours": 0}, headers={"Origin": "https://home.hahbah.com"})
    assert r.json()["muted"] is False and stored(app, "mute_until") == "" and stored(app, "mute_by") == ""


def test_bad_hours_rejected(app):
    c = signed_in(app)
    for bad in (2, -1, 25, "x", None):
        assert c.post("/api/mute", json={"hours": bad}, headers={"Origin": "https://home.hahbah.com"}).status_code == 400
    assert c.post("/api/mute", content=b"nope", headers={"Origin": "https://home.hahbah.com"}).status_code == 400
    assert stored(app, "mute_until") == ""


def test_cross_origin_rejected(app):
    c = signed_in(app)
    assert c.post("/api/mute", json={"hours": 1}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert stored(app, "mute_until") == ""


def test_settings_buttons_record_mute_by(app):
    make_user(app, "admin")
    from app.models import User
    with app.state.SessionLocal() as db:
        db.query(User).update({"is_admin": True})
        db.commit()
    c = TestClient(app)
    sign_in(c, "admin")
    c.post("/settings", data={"action": "mute", "hours": "1"}, headers={"Origin": "https://home.hahbah.com"})
    assert stored(app, "mute_by") == "admin" and stored(app, "mute_until")
    c.post("/settings", data={"action": "unmute"}, headers={"Origin": "https://home.hahbah.com"})
    assert stored(app, "mute_by") == "" and stored(app, "mute_until") == ""


def test_bell_renders_on_every_page_with_pill_while_muted(app):
    c = signed_in(app)
    for path in ("/", "/about", "/profile"):
        assert 'id="mutebell"' in c.get(path).text and "Muted by" not in c.get(path).text
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    put(app, "mute_until", until.strftime(ISO))
    put(app, "mute_by", "wife")
    for path in ("/", "/about", "/profile"):
        assert "Muted by wife until" in c.get(path).text


def test_pill_uses_home_time_zone(app):
    c = signed_in(app)
    put(app, "mute_until", "2099-01-01T20:40:00Z")   # 3:40 pm in New York (EST)
    put(app, "mute_by", "wife")
    assert "3:40 pm" in c.get("/about").text


def test_expired_mute_shows_unmuted(app):
    c = signed_in(app)
    put(app, "mute_until", (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(ISO))
    put(app, "mute_by", "wife")
    assert "Muted by" not in c.get("/about").text
    assert c.get("/api/snapshot").json()["mute"]["muted"] is False


def test_snapshot_carries_mute_for_the_poll(app):
    c = signed_in(app)
    put(app, "mute_until", "2099-01-01T20:40:00Z")
    put(app, "mute_by", "wife")
    m = c.get("/api/snapshot").json()["mute"]
    assert m["muted"] and m["by"] == "wife" and m["until"] == "2099-01-01T20:40:00Z" and "3:40 pm" in m["label"]
