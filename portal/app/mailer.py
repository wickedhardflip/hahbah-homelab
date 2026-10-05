"""The email outbox. The portal writes finished messages to /outbox; the collector (which holds the Gmail
app password via ~/.msmtprc) sends them. A message that can't be sent for a day moves to /outbox/failed.

The outbox is written by the web-facing portal, so its messages are not trusted: ALERT_EMAIL_TO is the collector's own
allowlist (comma-separated, first = primary), a message can only pick addresses from it (X-HAHBAH-To; none = the
primary), every address header in the file is replaced, and anything oversized is never sent."""
import email
import email.policy
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

GIVE_UP_S = 24 * 3600
MAX_BYTES = 2_000_000
ADDRESS_HEADERS = ("To", "Cc", "Bcc", "Resent-To", "Resent-Cc", "Resent-Bcc", "Reply-To")
MAX_WEB = 5               # addresses added from the web (the owners in alerts.env are extra)
MAX_FILE = 4096           # recipients.json is written by the web-facing portal: bigger than this is ignored
ADDRESS_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")   # same rule as deploy/alerts-env.sh
PICK_HEADER = "X-HAHBAH-To"   # the addresses the portal asks for (filtered against the allowlist here)


def parse_recipients(value) -> tuple:
    """'a@x, b@y' (or a list) -> ('a@x', 'b@y'): trimmed, lower-cased, no duplicates, order kept."""
    items = value.split(",") if isinstance(value, str) else list(value or ())
    out: list = []
    for a in (i.strip().lower() for i in items):
        if a and "@" in a and a not in out:
            out.append(a)
    return tuple(out)


def valid_address(a) -> bool:
    return isinstance(a, str) and len(a) <= 254 and ADDRESS_RE.fullmatch(a) is not None   # fullmatch: no trailing newline


def clean_web(items) -> tuple:
    """Strictly valid, lower-cased, unique, at most MAX_WEB: what the web may add to the allowlist."""
    out: list = []
    for a in items or ():
        a = a.lower() if isinstance(a, str) else a
        if valid_address(a) and a not in out:
            out.append(a)
    return tuple(out[:MAX_WEB])


def read_mail_config(path) -> tuple:
    """The portal-written /mail/recipients.json, re-validated on every read: (web-added addresses, owner addresses switched
    off, chosen primary). Missing, unreadable, oversized or odd = ((), (), "")."""
    try:
        p = Path(path)
        if p.stat().st_size > MAX_FILE:
            return (), (), ""
        doc = json.loads(p.read_bytes()[:MAX_FILE + 1].decode("utf-8"))
        items = doc["addresses"]
        web = clean_web(items) if isinstance(items, list) else ()
        off = parse_recipients(doc.get("owners_off")) if isinstance(doc.get("owners_off"), list) else ()
        primary = doc.get("primary")
        return web, off, primary.strip().lower() if isinstance(primary, str) else ""
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return (), (), ""


def read_web_recipients(path) -> tuple:
    return read_mail_config(path)[0]


def write_web_recipients(path, addrs, owners_off=(), primary="") -> None:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    doc = {"addresses": list(clean_web(addrs)), "owners_off": list(parse_recipients(owners_off)), "primary": primary or ""}
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(p)


def effective(owners, web=(), off=(), primary="") -> tuple:
    """The allowlist, first = primary. Owners (alerts.env) minus the ones switched off on the Settings page, then the web-added
    addresses; the chosen primary moves to the front if it is on the list. If nothing is left the owners come back, so the
    server can never end up with nobody to tell."""
    own = parse_recipients(owners)
    off = set(off)
    out = tuple(a for a in own if a not in off) + tuple(a for a in clean_web(web) if a not in own)
    if not out:
        return own
    return ((primary,) + tuple(a for a in out if a != primary)) if primary in out else out


def allowlist(owners, path=None) -> tuple:
    """Owners (alerts.env) and the web-added addresses, as the Settings page has arranged them."""
    return effective(owners, *read_mail_config(path)) if path else effective(owners)


def msmtp_send(raw: bytes, to: str) -> bool:
    try:   # the recipient is an argument, never read from the message (no -t)
        return subprocess.run(["msmtp", "--", to], input=raw, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _readdress(raw: bytes, to: str) -> bytes:
    m = email.message_from_bytes(raw, policy=email.policy.SMTP)
    for h in ADDRESS_HEADERS + (PICK_HEADER,):
        del m[h]
    m["To"] = to   # each recipient gets their own copy: nobody sees the other addresses
    return m.as_bytes()


def _picked(raw: bytes, allowed: tuple) -> tuple:
    m = email.message_from_bytes(raw, policy=email.policy.SMTP)
    asked = m.get(PICK_HEADER)
    if asked is None:
        return allowed[:1]
    want = set(parse_recipients(str(asked)))
    return tuple(a for a in allowed if a in want)


def _keep_only(msg: Path, raw: bytes, left: tuple) -> None:
    """After a partial send: the file keeps only the addresses still owed it (and its age, so it still gives up)."""
    m = email.message_from_bytes(raw, policy=email.policy.SMTP)
    del m[PICK_HEADER]
    m[PICK_HEADER] = ", ".join(left)
    st = msg.stat()
    tmp = msg.with_name(msg.name + ".tmp")
    tmp.write_bytes(m.as_bytes())
    os.utime(tmp, (st.st_atime, st.st_mtime))
    tmp.replace(msg)


def _age_order(p: Path):
    """Messages are named <kind>-<time_ns>.eml: send in the order they were written, whatever the kind."""
    tail = p.stem.rsplit("-", 1)[-1]
    return (int(tail) if tail.isdigit() else 0, p.name)


def _give_up(outbox: Path, msg: Path) -> None:
    (outbox / "failed").mkdir(parents=True, exist_ok=True)
    shutil.move(str(msg), str(outbox / "failed" / msg.name))


def send_outbox(outbox: Path, to, send=msmtp_send, clock=time.time, recipients_file=None) -> dict:
    """The allowlist is `to` (ALERT_EMAIL_TO, the owners) plus recipients_file (web-added, re-read each cycle). A message counts as sent once every address it picked has it."""
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)   # a deleted directory is recreated, not a silent no-op
    allowed = allowlist(to, recipients_file)
    stats = {"sent": 0, "queued": 0, "failed": 0}
    for msg in sorted(outbox.glob("*.eml"), key=_age_order):
        if not allowed:
            stats["queued"] += 1
            continue
        if msg.stat().st_size > MAX_BYTES:
            _give_up(outbox, msg)
            stats["failed"] += 1
            continue
        raw = msg.read_bytes()
        picked = _picked(raw, allowed)
        if not picked:   # asked only for addresses that aren't on the allowlist
            _give_up(outbox, msg)
            stats["failed"] += 1
            continue
        left = tuple(a for a in picked if not send(_readdress(raw, a), a))
        if not left:
            msg.unlink()
            stats["sent"] += 1
            continue
        if len(left) < len(picked):
            _keep_only(msg, raw, left)
        if clock() - msg.stat().st_mtime > GIVE_UP_S:
            _give_up(outbox, msg)
            stats["failed"] += 1
        else:
            stats["queued"] += 1
    return stats


def write_message(outbox: Path, raw: bytes, prefix: str) -> Path:
    """The portal's side: drop one finished message in the outbox (temp file + rename, so the sender never sees half)."""
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)   # umask 077 (set by the process) keeps it owner-only
    name = f"{prefix}-{time.time_ns()}.eml"
    tmp = outbox / (name + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(outbox / name)
    return outbox / name
