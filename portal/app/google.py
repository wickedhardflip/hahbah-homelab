"""Sign in with Google (OpenID Connect through Authlib). The portal only uses the verified email from the ID token."""
import re

import httpx
from authlib.common.errors import AuthlibBaseError
from authlib.integrations.base_client.errors import MismatchingStateError
import time

from authlib.integrations.starlette_client import OAuth
from authlib.integrations.starlette_client.integration import StarletteIntegration

DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"


class GoogleCancelled(Exception):
    """The person pressed Cancel on Google's screen."""


class GoogleStateError(Exception):
    """The callback didn't match a sign-in we started (expired cookie, bookmark, back button)."""


class GoogleUnavailable(Exception):
    """Google couldn't be reached, or its answer didn't check out."""


class _KeepRecentStates(StarletteIntegration):
    """Authlib forgets every earlier pending sign-in when a new one starts, so a second tab breaks the first.
    Keep the four most recent instead (each still expires after Authlib's normal hour)."""
    KEEP = 4

    async def set_state_data(self, session, state, data):
        prefix = f"_state_{self.name}_"
        now = time.time()
        for k in [k for k in session if k.startswith(prefix) and session[k].get("exp", 0) < now]:
            session.pop(k, None)
        session[f"{prefix}{state}"] = {"data": data, "exp": now + self.expires_in}
        for k in [k for k in session if k.startswith(prefix)][:-self.KEEP]:
            session.pop(k, None)


class _OAuth(OAuth):
    framework_integration_cls = _KeepRecentStates


class GoogleLogin:
    def __init__(self, client_id: str, client_secret: str):
        oauth = _OAuth()
        oauth.register("google", client_id=client_id, client_secret=client_secret,
                       server_metadata_url=DISCOVERY, client_kwargs={"scope": "openid email"})
        self.client = oauth.google

    async def redirect(self, request, redirect_uri: str, state: str | None = None):
        try:
            return await self.client.authorize_redirect(request, redirect_uri, prompt="select_account", state=state)
        except (httpx.HTTPError, AuthlibBaseError, ValueError, RuntimeError) as e:   # ValueError: discovery wasn't JSON
            raise GoogleUnavailable(type(e).__name__) from None

    async def identity(self, request) -> tuple[str, bool]:
        """(lower-cased email or "", email_verified) from Google's checked ID token."""
        error = request.query_params.get("error")
        if error == "access_denied":
            raise GoogleCancelled()
        if error:   # only a well-formed OAuth error code reaches the log, never raw query text
            raise GoogleUnavailable(f"google error {error if re.fullmatch(r'[a-z_]{1,40}', error) else 'unexpected'}")
        try:
            token = await self.client.authorize_access_token(request)
        except MismatchingStateError:
            raise GoogleStateError() from None
        except (httpx.HTTPError, AuthlibBaseError, ValueError) as e:
            raise GoogleUnavailable(type(e).__name__) from None
        info = token.get("userinfo") or {}
        return str(info.get("email") or "").strip().lower(), info.get("email_verified") is True
