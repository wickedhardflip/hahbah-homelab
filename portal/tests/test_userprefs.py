import logging

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app import settingsstore
from app.main import create_app
from app.models import User, UserPrefs
from conftest import FakeBuilder, make_user, sign_in
from test_recorder import DOWN, env, mails, snap, tick  # noqa: F401  (env is a fixture)

ALEX, WIFE = "alex@example.com", "wife@example.com"


def add_user(SL, name, google=None, **prefs):
    with SL() as db:
        u = User(username=name, password_hash="x", google_email=google)
        db.add(u)
        db.flush()
        if prefs:
            db.add(UserPrefs(user_id=u.id, **{"digest": True, "instant": True, "theme": "", **prefs}))
        db.commit()


def two_owners(env):
    rec, SL, clock, outbox = env
    rec.owners = (ALEX, WIFE)
    return rec, SL, clock, outbox


# ----- the mailer honours the prefs (they only subtract) -----
def test_digest_off_drops_the_user_from_the_digest_only(env):
    rec, SL, *_ = two_owners(env)
    add_user(SL, "wife", WIFE, digest=False)
    assert rec.picks("digest") == (ALEX,)
    assert rec.picks("danger") == rec.picks("clear") == (ALEX, WIFE)
    assert rec.picks("test") == rec.picks("notice") == (ALEX, WIFE)   # prefs don't touch test emails or notices


def test_instant_off_drops_the_user_from_danger_and_all_clear(env):
    rec, SL, clock, outbox = two_owners(env)
    add_user(SL, "wife", WIFE, instant=False)
    assert rec.picks("danger") == rec.picks("clear") == (ALEX,)
    assert rec.picks("digest") == (ALEX, WIFE)
    tick(rec, clock, snap([DOWN]), 3)
    (m,) = mails(outbox)
    assert m["X-HAHBAH-To"] == ALEX


def test_username_that_is_an_address_maps_too_and_users_without_email_are_unaffected(env):
    rec, SL, *_ = two_owners(env)
    add_user(SL, "Wife@Example.com", instant=False)
    add_user(SL, "kid", digest=False, instant=False)   # no email: changes nothing
    assert rec.picks("danger") == (ALEX,) and rec.picks("digest") == (ALEX, WIFE)


def test_settings_switches_still_apply_and_prefs_cannot_add_anyone(env):
    rec, SL, *_ = two_owners(env)
    add_user(SL, "stranger", "stranger@example.com")   # wants everything but isn't on the list
    with SL() as db:
        settingsstore.put(db, "report_off", WIFE)
    assert rec.picks("digest") == (ALEX,) and rec.picks("danger") == (ALEX, WIFE)


def test_shared_address_stays_if_any_user_still_wants_it(env):
    rec, SL, *_ = two_owners(env)
    add_user(SL, "wife", WIFE, instant=False)
    add_user(SL, WIFE)   # a second account named by the address, defaults (wants everything)
    assert rec.picks("danger") == (ALEX, WIFE)


def test_danger_falls_back_to_the_list_when_prefs_would_remove_everyone(env, caplog):
    rec, SL, clock, outbox = env
    add_user(SL, "alex", ALEX, instant=False, digest=False)
    with caplog.at_level(logging.WARNING, logger="portal"):
        assert rec.picks("danger") == (ALEX,) and rec.picks("clear") == (ALEX,)
    assert "profile" in caplog.text
    tick(rec, clock, snap([DOWN]), 3)
    assert len(mails(outbox)) == 1


def test_digest_with_everyone_opted_out_is_skipped_and_logged(env, caplog):
    rec, SL, *_ = env
    add_user(SL, "alex", ALEX, digest=False)
    with caplog.at_level(logging.WARNING, logger="portal"):
        assert rec.picks("digest") == ()
    assert "digest" in caplog.text


def test_settings_switching_everyone_off_is_not_overridden(env):
    rec, SL, *_ = env
    with SL() as db:
        settingsstore.put(db, "alerts_off", ALEX)
    assert rec.picks("danger") == ()   # an admin's choice on Settings, not a profile pref: no fallback


# ----- the table is created on startup for an existing database -----
def test_user_prefs_table_is_created_on_an_existing_db(settings):
    app = create_app(settings, builder=FakeBuilder(), collect=False)
    with app.state.SessionLocal() as db:
        db.execute(text("DROP TABLE user_prefs"))
        db.commit()
    app = create_app(settings, builder=FakeBuilder(), collect=False)
    cols = {c["name"] for c in inspect(app.state.SessionLocal.kw["bind"]).get_columns("user_prefs")}
    assert {"user_id", "digest", "instant", "theme"} <= cols


# ----- the Profile page -----
def signed_in(app):
    make_user(app)
    c = TestClient(app)
    sign_in(c)
    return c


def prefs(app):
    with app.state.SessionLocal() as db:
        return db.get(UserPrefs, db.query(User).first().id)


def test_prefs_post_needs_sign_in_and_same_origin(app):
    c = TestClient(app)
    assert c.post("/profile/prefs", data={"theme": "day"}, follow_redirects=False).status_code in (303, 401)
    c = signed_in(app)
    r = c.post("/profile/prefs", data={"digest": "1"}, headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and prefs(app) is None


def test_prefs_post_saves_and_page_shows_them(app):
    c = signed_in(app)
    html = c.get("/profile").text
    assert "Notifications" in html and 'name="digest"' in html and 'name="instant"' in html
    r = c.post("/profile/prefs", data={"instant": "1", "theme": "midnight"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/profile?saved=prefs"
    p = prefs(app)
    assert (p.digest, p.instant, p.theme) == (False, True, "midnight")
    c.post("/profile/prefs", data={"digest": "1", "instant": "1", "theme": ""})
    p = prefs(app)
    assert (p.digest, p.instant, p.theme) == (True, True, "")


def test_prefs_post_rejects_unknown_theme(app):
    c = signed_in(app)
    assert c.post("/profile/prefs", data={"theme": "neon"}, follow_redirects=False).status_code == 400
    assert prefs(app) is None


def test_default_theme_renders_on_every_page(app):
    c = signed_in(app)
    with app.state.SessionLocal() as db:
        db.query(User).update({"is_admin": True})
        db.commit()
    for path in ("/", "/about", "/settings", "/profile"):
        assert 'data-theme="day"' in c.get(path).text, path
    c.post("/profile/prefs", data={"digest": "1", "instant": "1", "theme": "midnight"})
    for path in ("/", "/about", "/settings", "/profile"):
        assert '<html lang="en" data-theme="midnight">' in c.get(path).text, path
