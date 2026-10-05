import asyncio

import httpx
import pytest
from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.jose.errors import InvalidClaimError
from starlette.requests import Request

from app.config import Settings
from app.google import GoogleCancelled, GoogleLogin, GoogleStateError, GoogleUnavailable


def req(query=b""):
    return Request({"type": "http", "method": "GET", "path": "/auth/google/callback", "query_string": query,
                    "headers": [], "session": {}})


class Stub:
    def __init__(self, result):
        self.result = result

    async def authorize_access_token(self, request):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def google_with(result):
    g = GoogleLogin("id.apps.googleusercontent.com", "not-a-real-secret")
    g.client = Stub(result)
    return g


def identity(g, query=b""):
    return asyncio.run(g.identity(req(query)))


def test_identity_lower_cases_and_reads_verified():
    g = google_with({"userinfo": {"email": " Alex@Example.COM ", "email_verified": True}})
    assert identity(g) == ("alex@example.com", True)


def test_identity_without_email_or_verified():
    assert identity(google_with({"userinfo": {}})) == ("", False)
    assert identity(google_with({"userinfo": {"email": "a@b.c", "email_verified": "true"}})) == ("a@b.c", False)


def test_cancel_at_google():
    with pytest.raises(GoogleCancelled):
        identity(google_with({}), b"error=access_denied&state=x")


def test_other_google_error_is_unavailable():
    with pytest.raises(GoogleUnavailable):
        identity(google_with({}), b"error=server_error")


@pytest.mark.parametrize("exc,expected", [
    (MismatchingStateError(), GoogleStateError),
    (httpx.ConnectError("boom"), GoogleUnavailable),
    (InvalidClaimError("aud"), GoogleUnavailable),
])
def test_failures_map_to_plain_errors(exc, expected):
    with pytest.raises(expected):
        identity(google_with(exc))


def test_settings_repr_hides_google_secrets(tmp_path):
    s = Settings(data_dir=tmp_path, host_proc=tmp_path, host_sys=tmp_path, disks=(), nic="x", eero_igd_url="",
                 deploy_log=tmp_path, apps_file=tmp_path, cookie_name="hl_session", cookie_domain=None,
                 cookie_secure=False, session_days=30, collect_interval=30, base_domain="hahbah.com",
                 home_url="https://home.hahbah.com", google_client_id="id.apps.googleusercontent.com",
                 google_client_secret="SECRET-VALUE-1", oauth_state_secret="SECRET-VALUE-2")
    assert "SECRET-VALUE" not in repr(s) and s.google_configured
    assert not Settings(**{**s.__dict__, "google_client_secret": ""}).google_configured


def test_odd_error_codes_are_not_logged_verbatim():
    with pytest.raises(GoogleUnavailable) as e:
        identity(google_with({}), b"error=x%0AFAKE+LOG+LINE")
    assert chr(10) not in str(e.value) and "FAKE" not in str(e.value)


class BrokenMetadata:
    async def authorize_redirect(self, request, redirect_uri, **kw):
        raise ValueError("Expecting value: line 1 column 1")   # discovery answered 200 with a non-JSON body


def test_garbled_discovery_is_unavailable_not_a_crash():
    g = GoogleLogin("id.apps.googleusercontent.com", "not-a-real-secret")
    g.client = BrokenMetadata()
    with pytest.raises(GoogleUnavailable):
        asyncio.run(g.redirect(req(), "https://home.hahbah.com/auth/google/callback", state="s"))


def test_real_authlib_client_uses_our_state():
    import time
    g = GoogleLogin("id.apps.googleusercontent.com", "not-a-real-secret")
    g.client.server_metadata.update({"issuer": "https://accounts.google.com", "_loaded_at": time.time(),
                                     "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                                     "token_endpoint": "https://oauth2.googleapis.com/token",
                                     "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"})
    r = req()
    resp = asyncio.run(g.redirect(r, "https://home.hahbah.com/auth/google/callback", state="tab-one-state"))
    assert "state=tab-one-state" in resp.headers["location"] and "prompt=select_account" in resp.headers["location"]
    assert any(k.endswith("tab-one-state") for k in r.session)


def test_real_authlib_keeps_both_tabs_states():
    import time
    g = GoogleLogin("id.apps.googleusercontent.com", "not-a-real-secret")
    g.client.server_metadata.update({"issuer": "https://accounts.google.com", "_loaded_at": time.time(),
                                     "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                                     "token_endpoint": "https://oauth2.googleapis.com/token",
                                     "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"})
    r = req()
    for s in ("tab-one", "tab-two"):
        asyncio.run(g.redirect(r, "https://home.hahbah.com/auth/google/callback", state=s))
    assert asyncio.run(g.client.framework.get_state_data(r.session, "tab-one")) is not None
    assert asyncio.run(g.client.framework.get_state_data(r.session, "tab-two")) is not None
    for i in range(6):   # still bounded: only the four newest pending sign-ins are kept
        asyncio.run(g.redirect(r, "https://home.hahbah.com/auth/google/callback", state=f"more-{i}"))
    assert len([k for k in r.session if k.startswith("_state_google_")]) == 4
