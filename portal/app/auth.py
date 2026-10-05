"""Passwords (Argon2id), sessions, login rate limiting and the signed-in-user dependency."""
import hashlib
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, Request
from sqlalchemy.orm import Session as DB

from .db import get_db
from . import settingsstore
from .models import Session, User, utcnow

_ph = PasswordHasher()
_HOST = re.compile(r"[a-z0-9.-]+")
_DUMMY_HASH = _ph.hash("not-a-real-password")
TOUCH_EVERY = timedelta(minutes=1)   # last_seen is rewritten at most this often, not on every request


class NotSignedIn(Exception):
    pass


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def normalize(username: str) -> str:
    return username.strip().lower()


def authenticate(db: DB, username: str, password: str) -> User | None:
    user = db.query(User).filter(User.username == normalize(username)).first()
    if not user:
        verify_password(password, _DUMMY_HASH)  # same cost whether or not the user exists
        return None
    return user if verify_password(password, user.password_hash) else None


def _session_id(token: str) -> str:
    """The database keeps only a hash of each cookie token, so a copied database can't sign anyone in."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: DB, user: User, days: int) -> str:
    """Returns the cookie token; the row stores its hash."""
    token = secrets.token_hex(32)
    db.add(Session(id=_session_id(token), user_id=user.id, expires_at=utcnow() + timedelta(days=days),
                   last_seen=utcnow()))
    db.commit()
    return token


def delete_session(db: DB, token: str) -> None:
    db.query(Session).filter(Session.id == _session_id(token)).delete()
    db.commit()


class LoginLimiter:
    """Refuse sign-ins from an address after `limit` failures within `window` seconds."""

    def __init__(self, limit: int = 5, window: float = 900, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._fails = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str) -> deque:
        q, now = self._fails[key], self.clock()
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def try_acquire(self, key: str) -> bool:
        """Count this attempt before the (slow) password check, so parallel guesses can't slip past."""
        with self._lock:
            q = self._recent(key)
            if len(q) >= self.limit:
                return False
            q.append(self.clock())
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


def user_from_request(request: Request, db: DB) -> User | None:
    token = request.cookies.get(request.app.state.settings.cookie_name)
    session = db.get(Session, _session_id(token)) if token else None
    if not session:
        return None
    now = utcnow()
    last = session.last_seen or session.created_at
    if session.is_expired or now - last > timedelta(minutes=settingsstore.idle_minutes(db)):
        db.delete(session)
        db.commit()
        return None
    if session.last_seen is None or now - session.last_seen >= TOUCH_EVERY:
        session.last_seen = now
        db.commit()
    return db.get(User, session.user_id)


def current_user(request: Request, db: DB = Depends(get_db)) -> User:
    user = user_from_request(request, db)
    if not user:
        raise NotSignedIn()
    return user


def safe_next(url: str | None, domain: str) -> str | None:
    """Where to send someone after sign-in: a path on this site, or an https page on our own domain.
    The result is rebuilt from the parsed parts (no userinfo, no fragment), never the raw input."""
    if not url or not url.isprintable() or any(c in url for c in "\\ "):
        return None  # browsers read "\" as "/", so https://evil.com\.hahbah.com would leave the domain
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme and not parts.netloc:   # relative: one leading slash ("//host" is another site)
        if not parts.path.startswith("/") or parts.path.startswith("//"):
            return None
        return urlunsplit(("", "", parts.path, parts.query, ""))
    host, domain = (parts.hostname or "").lower(), domain.lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    if not _HOST.fullmatch(host) or (host != domain and not host.endswith("." + domain)):
        return None
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def same_origin(request: Request, home_url: str) -> bool:
    """Form posts must come from the portal's own pages (other *.hahbah.com apps count as same-site for cookies).
    Browsers always send Origin on POST; a missing one (curl, tests) is allowed."""
    origin = request.headers.get("origin")
    return origin is None or origin == home_url
