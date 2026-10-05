from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_user, sign_in


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


def test_dashboard_redirects_to_login_when_signed_out(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_good_password_sets_session_cookie_with_safe_flags(app, client):
    make_user(app)
    r = sign_in(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "hl_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie


def test_username_is_case_insensitive(app, client):
    make_user(app)
    assert sign_in(client, username="  Alex ").status_code == 303


def test_wrong_password_is_refused_without_saying_which_part(app, client):
    make_user(app)
    r = sign_in(client, password="nope")
    assert r.status_code == 401
    assert "Wrong username or password" in r.text
    assert "hl_session" not in r.headers.get("set-cookie", "")


def test_five_failures_then_429_even_with_right_password(app, client):
    make_user(app)
    for _ in range(5):
        assert sign_in(client, password="nope").status_code == 401
    r = sign_in(client)
    assert r.status_code == 429
    assert "Too many" in r.text


def test_expired_session_is_rejected(app, client):
    from app.models import Session
    make_user(app)
    sign_in(client)
    with app.state.SessionLocal() as db:
        s = db.query(Session).one()
        s.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.commit()
    assert client.get("/", follow_redirects=False).status_code == 303


def test_logout_ends_the_session(app, client):
    from app.models import Session
    make_user(app)
    sign_in(client)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    with app.state.SessionLocal() as db:
        assert db.query(Session).count() == 0
    assert client.get("/", follow_redirects=False).status_code == 303


def test_parallel_guesses_cannot_exceed_the_limit():
    import threading
    from app.auth import LoginLimiter
    limiter, barrier, results = LoginLimiter(limit=5), threading.Barrier(30), []
    def attempt():
        barrier.wait()
        results.append(limiter.try_acquire("10.0.0.9"))
    threads = [threading.Thread(target=attempt) for _ in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 5


def test_successful_sign_in_clears_the_counter(app, client):
    make_user(app)
    for _ in range(4):
        sign_in(client, password="nope")
    assert sign_in(client).status_code == 303
    for _ in range(4):
        assert sign_in(client, password="nope").status_code == 401


def test_login_page_uses_the_platform_background(client):
    page = client.get("/login").text
    assert "/static/login/platform-1920.jpg" in page and "/static/login/platform-1100.jpg" in page
    img = client.get("/static/login/platform-1920.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"


def test_login_page_plays_the_platform_video_with_the_photo_as_fallback(client):
    page = client.get("/login").text
    assert '<source src="/static/login/platform-loop.mp4" type="video/mp4">' in page
    assert 'poster="/static/login/platform-1920.jpg"' in page and "muted" in page and "loop" in page
    assert "prefers-reduced-motion" in page          # still image for people who turn motion off
    vid = client.get("/static/login/platform-loop.mp4")
    assert vid.status_code == 200 and vid.headers["content-type"] == "video/mp4"


from urllib.parse import quote

import pytest

FWD = {"X-Forwarded-Host": "jobs.hahbah.com", "X-Forwarded-Uri": "/jobs?tab=new", "X-Forwarded-Method": "GET"}


def test_verify_signed_out_redirects_to_login_with_the_original_url(client):
    r = client.get("/auth/verify", headers=FWD, follow_redirects=False)
    assert r.status_code == 302 and r.headers["cache-control"] == "no-store"
    assert r.headers["location"] == "https://home.hahbah.com/login?next=" + quote("https://jobs.hahbah.com/jobs?tab=new", safe="")


def test_verify_signed_in_says_yes_with_the_user(app, client):
    make_user(app)
    sign_in(client)
    r = client.get("/auth/verify", headers=FWD, follow_redirects=False)
    assert r.status_code == 200 and r.headers["remote-user"] == "alex" and r.headers["cache-control"] == "no-store"


def test_verify_forged_cookie_is_signed_out(client):
    client.cookies.set("hl_session", "f" * 64)
    assert client.get("/auth/verify", headers=FWD, follow_redirects=False).status_code == 302


def test_sign_in_returns_to_next(app, client):
    make_user(app)
    r = client.post("/login", data={"username": "alex", "password": "correct horse battery",
                                    "next": "https://jobs.hahbah.com/jobs?tab=new"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://jobs.hahbah.com/jobs?tab=new"


@pytest.mark.parametrize("evil", ["https://evil.com/", "//evil.com", "https://hahbah.com.evil.com/", "http://jobs.hahbah.com/",
                                  "https://user@evil.com/", "javascript:alert(1)", ""])
def test_unsafe_next_falls_back_to_home(app, client, evil):
    make_user(app)
    r = client.post("/login", data={"username": "alex", "password": "correct horse battery", "next": evil}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_login_page_carries_next_and_signed_in_visitors_skip_it(app, client):
    nxt = "https://replexon.hahbah.com/"
    assert f'value="{nxt}"' in client.get("/login", params={"next": nxt}).text
    make_user(app)
    sign_in(client)
    r = client.get("/login", params={"next": nxt}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == nxt


def test_get_logout_asks_before_signing_out_of_every_app(app, client):
    from app.models import Session
    make_user(app)
    sign_in(client)
    r = client.get("/logout")
    assert r.status_code == 200 and 'action="/logout"' in r.text and 'method="post"' in r.text
    with app.state.SessionLocal() as db:
        assert db.query(Session).count() == 1          # a GET never signs you out by itself


def test_get_logout_when_signed_out_goes_to_sign_in(client):
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("evil", [r"https://evil.com\.hahbah.com/", r"https://evil.com\@jobs.hahbah.com/",
                                  "https://jobs.hahbah.com/\n", "https://jobs.hahbah.com /", r"https://evil.com\\.hahbah.com/"])
def test_backslash_and_control_char_next_is_refused(app, client, evil):
    make_user(app)
    r = client.post("/login", data={"username": "alex", "password": "correct horse battery", "next": evil}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_login_from_another_origin_is_refused(app, client):
    make_user(app)
    r = client.post("/login", data={"username": "alex", "password": "correct horse battery"},
                    headers={"Origin": "https://jobs.hahbah.com"}, follow_redirects=False)
    assert r.status_code == 403 and "hl_session" not in r.headers.get("set-cookie", "")
    r = client.post("/login", data={"username": "alex", "password": "correct horse battery"},
                    headers={"Origin": "https://home.hahbah.com"}, follow_redirects=False)
    assert r.status_code == 303


def test_logout_from_another_origin_is_refused(app, client):
    make_user(app)
    sign_in(client)
    assert client.post("/logout", headers={"Origin": "https://evil.com"}, follow_redirects=False).status_code == 403
    assert client.get("/", follow_redirects=False).status_code == 200


def test_database_keeps_only_a_hash_of_the_session_token(app, client):
    from app.models import Session
    make_user(app)
    sign_in(client)
    token = client.cookies.get("hl_session")
    with app.state.SessionLocal() as db:
        ids = [s.id for s in db.query(Session).all()]
    assert token and ids and token not in ids
    client.cookies.set("hl_session", ids[0])  # the stored value itself is not a working cookie
    assert client.get("/", follow_redirects=False).status_code == 303


# ---- idle timeout (sliding) ----

def _age(app, **ago):
    """Pretend the session was last used `ago` ago."""
    from app.models import Session, utcnow
    with app.state.SessionLocal() as db:
        s = db.query(Session).one()
        s.last_seen = utcnow() - timedelta(**ago)
        db.commit()


def _last_seen(app):
    from app.models import Session
    with app.state.SessionLocal() as db:
        return db.query(Session).one().last_seen


def test_idle_session_expires_after_the_configured_minutes(app, client):
    from app import settingsstore
    make_user(app)
    sign_in(client)
    _age(app, minutes=479)
    assert client.get("/", follow_redirects=False).status_code == 200      # default 480
    _age(app, minutes=481)
    assert client.get("/", follow_redirects=False).status_code == 303
    with app.state.SessionLocal() as db:
        settingsstore.put(db, "idle_minutes", "15")
    sign_in(client)
    _age(app, minutes=16)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_activity_slides_the_window_but_writes_at_most_once_a_minute(app, client):
    make_user(app)
    sign_in(client)
    _age(app, seconds=30)
    before = _last_seen(app)
    assert client.get("/").status_code == 200
    assert _last_seen(app) == before                  # under a minute: no write
    _age(app, minutes=10)
    before = _last_seen(app)
    assert client.get("/").status_code == 200
    assert _last_seen(app) > before                   # slid forward


def test_legacy_session_without_last_seen_uses_created_at(app, client):
    from app.models import Session
    make_user(app)
    sign_in(client)
    with app.state.SessionLocal() as db:
        s = db.query(Session).one()
        s.last_seen = None
        db.commit()
    assert client.get("/", follow_redirects=False).status_code == 200


def test_htmx_request_with_no_session_gets_401_and_hx_redirect(client):
    r = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"


@pytest.mark.parametrize("url,want", [
    ("https://jobs.hahbah.com/jobs?tab=new#frag", "https://jobs.hahbah.com/jobs?tab=new"),
    ("https://JOBS.HahBah.com:443/x", "https://jobs.hahbah.com/x"),
    ("https://hahbah.com", "https://hahbah.com/"),
    ("/settings?saved=1", "/settings?saved=1"),
    ("https://evil.com/", None), ("//evil.com/x", None), ("/\\evil.com", None), ("https://jobs.hahbah.com:8443/", None),
    ("https://jobs.hahbah.com@evil.com/", None), ("https://exa_mple.hahbah.com/", None), ("https://hahbah.com.evil.com/", None),
    ("jobs.hahbah.com/x", None), ("https://[::1]/", None), ("https://jobs.hahbah.com:bad/", None), ("http://jobs.hahbah.com/", None),
])
def test_safe_next_rebuilds_the_url_or_refuses(url, want):
    from app.auth import safe_next
    assert safe_next(url, "hahbah.com") == want
