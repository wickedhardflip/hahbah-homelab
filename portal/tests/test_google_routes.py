from dataclasses import replace

import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from app.google import GoogleCancelled, GoogleStateError, GoogleUnavailable
from app.main import create_app
from conftest import FakeBuilder, make_user, sign_in


class FakeGoogle:
    def __init__(self, email="alex@example.com", verified=True, fail=None):
        self.email, self.verified, self.fail = email, verified, fail

    async def redirect(self, request, redirect_uri, state=None):
        self.redirect_uri, self.state = redirect_uri, state
        request.session[f"fake:{state}"] = 1   # like Authlib: one entry per sign-in, keyed by its state
        return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?state={state}", status_code=302)

    async def identity(self, request):
        if self.fail:
            raise self.fail
        if not request.session.pop(f"fake:{request.query_params.get('state')}", None):
            raise GoogleStateError()
        return self.email, self.verified


@pytest.fixture
def gapp(settings):
    s = replace(settings, google_client_id="id.apps.googleusercontent.com", google_client_secret="not-a-real-secret",
                oauth_state_secret="test-state-secret")
    app = create_app(s, builder=FakeBuilder(), collect=False)
    make_user(app)
    with app.state.SessionLocal() as db:
        from app.models import User
        db.query(User).one().google_email = "alex@example.com"
        db.commit()
    return app


def go(app, fake, next_url=None):
    app.state.google = fake
    c = TestClient(app)
    start = c.get("/auth/google", params={"next": next_url} if next_url else None, follow_redirects=False)
    return c, start, c.get(f"/auth/google/callback?code=x&state={fake.state}", follow_redirects=False)


def test_linked_account_signs_in_and_keeps_next(gapp):
    fake = FakeGoogle()
    c, start, cb = go(gapp, fake, "https://jobs.hahbah.com/deep")
    assert start.status_code == 302 and start.headers["location"].startswith("https://accounts.google.com/")
    assert fake.redirect_uri == "https://home.hahbah.com/auth/google/callback"
    assert cb.status_code == 303 and cb.headers["location"] == "https://jobs.hahbah.com/deep"
    assert c.get("/", follow_redirects=False).status_code == 200


def test_google_cookie_matches_password_cookie(gapp):
    _, _, cb = go(gapp, FakeGoogle())
    pw = sign_in(TestClient(gapp))
    def attrs(r):
        h = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("hl_session=")][0]
        return sorted(p.strip().lower() for p in h.split(";")[1:])
    assert attrs(cb) == attrs(pw)


def test_unlinked_or_unverified_is_refused(gapp):
    for fake in (FakeGoogle(email="someone@else.com"), FakeGoogle(verified=False)):
        c, _, cb = go(gapp, fake)
        assert cb.status_code == 403 and "isn&#39;t allowed here" in cb.text
        assert c.get("/", follow_redirects=False).status_code == 303


def test_missing_email_is_refused(gapp):
    with gapp.state.SessionLocal() as db:   # a user with no Google link must not match an empty email
        from app.models import User
        db.query(User).one().google_email = None
        db.commit()
    _, _, cb = go(gapp, FakeGoogle(email="", verified=True))
    assert cb.status_code == 403


@pytest.mark.parametrize("fail,status,text", [
    (GoogleCancelled(), 400, "was cancelled"),
    (GoogleUnavailable("ConnectError"), 502, "reach Google"),
])
def test_google_failures_show_the_login_page(gapp, fail, status, text):
    _, _, cb = go(gapp, FakeGoogle(fail=fail))
    assert cb.status_code == status and text in cb.text and "Sign in with Google" in cb.text


def test_callback_without_start_is_a_state_error(gapp):
    gapp.state.google = FakeGoogle()
    cb = TestClient(gapp).get("/auth/google/callback?code=x&state=s1", follow_redirects=False)
    assert cb.status_code == 400 and "took too long" in cb.text


def test_unsafe_next_is_dropped(gapp):
    _, _, cb = go(gapp, FakeGoogle(), "http://evil.example/")
    assert cb.status_code == 303 and cb.headers["location"] == "/"


def test_login_page_button_only_when_configured(gapp, client):
    gapp.state.google = FakeGoogle()
    page = TestClient(gapp).get("/login?next=https%3A%2F%2Fjobs.hahbah.com%2F").text
    assert 'href="/auth/google?next=https%3A//jobs.hahbah.com/"' in page or 'href="/auth/google?next=https%3A%2F%2Fjobs.hahbah.com%2F"' in page
    assert "Sign in with Google" not in client.get("/login").text
    assert client.get("/auth/google", follow_redirects=False).status_code == 404
    assert client.get("/auth/google/callback", follow_redirects=False).status_code == 404


def test_two_tabs_each_land_where_they_started(gapp):
    fake = FakeGoogle()
    gapp.state.google = fake
    c = TestClient(gapp)
    c.get("/auth/google", params={"next": "https://jobs.hahbah.com/a"}, follow_redirects=False)
    first = fake.state
    c.get("/auth/google", params={"next": "https://replexon.hahbah.com/b"}, follow_redirects=False)
    second = fake.state
    assert first != second
    cb1 = c.get(f"/auth/google/callback?code=x&state={first}", follow_redirects=False)
    cb2 = c.get(f"/auth/google/callback?code=x&state={second}", follow_redirects=False)
    assert cb1.headers["location"] == "https://jobs.hahbah.com/a" and cb2.headers["location"] == "https://replexon.hahbah.com/b"
