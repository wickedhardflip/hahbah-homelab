from fastapi.testclient import TestClient

from app.auth import verify_password
from app.models import Session, User
from conftest import make_user, sign_in

OLD, NEW = "correct horse battery", "a brand new passphrase"


def signed_in(app, **kw):
    make_user(app, **kw)
    c = TestClient(app)
    sign_in(c)
    return c


def pw(c, current=OLD, new=NEW, confirm=None, **kw):
    return c.post("/profile/password", data={"current": current, "new": new, "confirm": new if confirm is None else confirm},
                  follow_redirects=False, **kw)


def user_row(app, name="alex"):
    with app.state.SessionLocal() as db:
        u = db.query(User).filter(User.username == name).first()
        db.expunge(u)
        return u


def test_profile_needs_sign_in(client):
    assert client.get("/profile", follow_redirects=False).status_code == 303
    assert client.post("/profile/password", follow_redirects=False).status_code in (303, 401)
    assert client.post("/profile/google/unlink", follow_redirects=False).status_code in (303, 401)


def test_profile_page_shows_account(app):
    c = signed_in(app)
    with app.state.SessionLocal() as db:
        db.query(User).update({"google_email": "b@example.com", "is_admin": True})
        db.commit()
    html = c.get("/profile").text
    assert "alex" in html and "Admin" in html and "b@example.com" in html and "Member since" in html


def test_header_menu_on_every_page(app):
    c = signed_in(app)
    with app.state.SessionLocal() as db:
        db.query(User).update({"is_admin": True})
        db.commit()
    for path in ("/", "/about", "/settings", "/profile"):
        r = c.get(path)
        assert 'class="umenu"' in r.text, path
        assert 'href="/profile"' in r.text, path
        assert 'action="/logout"' in r.text, path


def test_settings_icon_admin_only(app):
    c = signed_in(app)
    assert 'href="/settings"' not in c.get("/profile").text
    with app.state.SessionLocal() as db:
        db.query(User).update({"is_admin": True})
        db.commit()
    assert 'href="/settings"' in c.get("/profile").text


def test_change_password(app):
    c = signed_in(app)
    assert pw(c).status_code == 303
    assert verify_password(NEW, user_row(app).password_hash)
    assert "Password changed" in c.get("/profile?saved=password").text


def test_change_password_signs_out_other_sessions_only(app):
    c = signed_in(app)
    other = TestClient(app)
    sign_in(other)
    assert pw(c).status_code == 303
    assert c.get("/profile", follow_redirects=False).status_code == 200
    assert other.get("/profile", follow_redirects=False).status_code == 303
    with app.state.SessionLocal() as db:
        assert db.query(Session).count() == 1


def test_change_password_rejections(app):
    c = signed_in(app)
    cases = [pw(c, current="wrong password!!"), pw(c, new="short", confirm="short"), pw(c, confirm="something different")]
    for r in cases:
        assert r.status_code == 400
    assert verify_password(OLD, user_row(app).password_hash)


def test_change_password_cross_origin_refused(app):
    c = signed_in(app)
    assert pw(c, headers={"origin": "https://evil.example"}).status_code == 403
    assert verify_password(OLD, user_row(app).password_hash)


def test_google_unlink(app):
    c = signed_in(app)
    with app.state.SessionLocal() as db:
        db.query(User).update({"google_email": "b@example.com"})
        db.commit()
    assert c.post("/profile/google/unlink", headers={"origin": "https://evil.example"}).status_code == 403
    assert user_row(app).google_email == "b@example.com"
    assert c.post("/profile/google/unlink", data={"current": "wrong"}, follow_redirects=False).status_code == 400
    assert user_row(app).google_email == "b@example.com"
    assert c.post("/profile/google/unlink", data={"current": OLD}, follow_redirects=False).status_code == 303
    assert user_row(app).google_email is None


def test_password_change_is_rate_limited(app):
    c = signed_in(app)
    codes = [pw(c, current="wrong").status_code for _ in range(12)]
    assert codes[0] == 400 and 429 in codes
    assert verify_password(OLD, user_row(app).password_hash)
