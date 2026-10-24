"""Wyze smart plugs through the unofficial wyze-sdk (cloud). Plugs only: list, on/off, away mode.

Gentle with the cloud API: one login per start (the token stays in memory), logged in again only after an auth error,
and a failed login waits 5 min, 10 min ... up to an hour before the next try. Nothing here retries; the sidecar's
slots are the only retries. Credentials come from the caller (environment variables) and are never logged or returned.
"""
import time

ACTIONS = ("on", "off", "away_on", "away_off")
BACKOFF_S, BACKOFF_MAX_S = 300, 3600


class Backoff(Exception):
    """Login is paused after a failure."""


def _default_factory(email: str, password: str, key_id: str, api_key: str):
    from wyze_sdk import Client   # imported late: tests and the portal never need the package
    c = Client()
    c.login(email=email, password=password, key_id=key_id, api_key=api_key)
    return c


def _is_auth_error(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}".lower()
    return any(w in text for w in ("token", "unauthorized", "login", "auth"))


def _row(p) -> dict:
    return {"id": p.mac, "model": p.product.model, "nickname": p.nickname or "", "online": bool(p.is_online),
            "on": bool(p.is_on), "away": bool(getattr(p, "away_mode", False))}


class Wyze:
    def __init__(self, email: str, password: str, key_id: str, api_key: str, factory=_default_factory, clock=time.monotonic):
        self._creds, self._factory, self._clock = (email, password, key_id, api_key), factory, clock
        self._client, self._fails, self._retry_at = None, 0, 0.0

    def __repr__(self) -> str:
        return "Wyze()"   # never show the credentials

    def _c(self):
        if self._client is None:
            if self._clock() < self._retry_at:
                raise Backoff()
            try:
                self._client = self._factory(*self._creds)
            except Exception:
                self._fails += 1
                self._retry_at = self._clock() + min(BACKOFF_S * 2 ** (self._fails - 1), BACKOFF_MAX_S)
                raise
            self._fails = 0
        return self._client

    def _call(self, fn):
        """Run one API call; an auth error drops the token so the next call logs in once more."""
        try:
            return fn(self._c())
        except Backoff:
            raise
        except Exception as e:
            if self._client is not None and _is_auth_error(e):
                self._client = None
            raise

    def list(self) -> list:
        return [_row(p) for p in self._call(lambda c: c.plugs.list())]

    def info(self, mac: str) -> dict | None:
        p = self._call(lambda c: c.plugs.info(device_mac=mac))
        return _row(p) if p else None

    def command(self, mac: str, model: str, action: str) -> None:
        if action not in ACTIONS:
            raise ValueError("unknown action")

        def run(c):
            if action in ("on", "off"):
                (c.plugs.turn_on if action == "on" else c.plugs.turn_off)(device_mac=mac, device_model=model)
            else:
                c.plugs.set_away_mode(device_mac=mac, device_model=model, away_mode=action == "away_on")
        self._call(run)
