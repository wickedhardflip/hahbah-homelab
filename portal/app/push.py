"""Web push for Danger alerts (spec: docs/specs/2026-10-24-mobile-pwa-design.md).

Subscriptions live in data_dir/push_subs.json. Off unless all three PORTAL_VAPID_* settings are present. The message is always the same
generic line, so a lock screen never shows an app or host name. pywebpush is imported only when something is actually sent.
"""
import json
import logging
import threading
from pathlib import Path

from .plugs import write_json

log = logging.getLogger("homelab.push")
MAX_SUBS = 20
PAYLOAD = {"title": "HAHBAH", "body": "A Danger alert opened. Tap to see it.", "url": "/m#alerts", "tag": "danger"}
_lock = threading.Lock()


def read(path: Path) -> dict:
    """{endpoint: {"keys": {...}, "user": name}}; a missing or damaged file is no subscriptions."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in doc.items() if isinstance(v, dict)} if isinstance(doc, dict) else {}


def valid(sub) -> bool:
    """A browser PushSubscription JSON: https endpoint plus p256dh and auth keys, all short strings."""
    if not isinstance(sub, dict) or not isinstance(sub.get("keys"), dict):
        return False
    ep, k = sub.get("endpoint"), sub["keys"]
    return (isinstance(ep, str) and ep.startswith("https://") and len(ep) <= 600
            and all(isinstance(k.get(n), str) and 0 < len(k[n]) <= 200 for n in ("p256dh", "auth")))


def add(path: Path, sub: dict, user: str) -> bool:
    with _lock:
        subs = read(path)
        if sub["endpoint"] not in subs and len(subs) >= MAX_SUBS:
            return False
        subs[sub["endpoint"]] = {"keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]}, "user": user}
        write_json(path, subs)
    return True


def remove(path: Path, endpoints) -> None:
    with _lock:
        subs = read(path)
        if any(subs.pop(e, None) is not None for e in endpoints):
            write_json(path, subs)


def _webpush(sub: dict, data: str, settings) -> None:
    from pywebpush import webpush
    webpush(subscription_info=sub, data=data, vapid_private_key=settings.vapid_private,
            vapid_claims={"sub": settings.vapid_subject}, ttl=3600)


def send_all(settings, path: Path, sender=None) -> int:
    """Send the Danger notice to every subscription; drop the ones the push service says are gone (404/410). Returns how many went out."""
    if not settings.push_configured:
        return 0
    sender = sender or _webpush
    sent, gone = 0, []
    for ep, rec in read(path).items():
        try:
            sender({"endpoint": ep, "keys": rec["keys"]}, json.dumps(PAYLOAD), settings)
            sent += 1
        except Exception as e:  # noqa: BLE001 (one dead phone must not stop the rest)
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                gone.append(ep)
            else:
                log.warning("push to one device failed: %s", type(e).__name__)
    remove(path, gone)
    return sent
