"""After every 30 s snapshot: open/close incidents, email Danger + all-clears, keep the daily numbers, send the digest.

An incident opens when a Caution or Danger alert shows in 2 checks in a row and closes after 2 checks without it, so a
single blip is never an incident. Only Danger is emailed. If it comes back within RECOVERY of closing, the same incident
reopens quietly; the all-clear goes out only after RECOVERY with no relapse, so the last email is never a false "all clear".
An incident whose source stopped reporting (station grey, or its collector file stale) stays open: no news is not good news.
Emails are queued only after the database commit, so a failed save never resends them.
Acknowledging an open incident (any signed-in user, from a card) skips its Danger email if not sent yet and its all-clear;
Caution->Danger drops the ack so the Danger email still goes out. A quiet reopen is the same incident, ack included.
A maintenance window (start <= now < end, not cancelled; target = the incident key or "*") holds Danger emails and
all-clears for what it covers, judged at each check by the recorder's clock. Held is not skipped: `emailed` stays False
like under a mute, so an incident still open when the window ends is emailed then; one that opened and closed inside
it was never emailed, so it gets no all-clear either.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import emails, settingsstore
from .mailer import MAX_WEB, PICK_HEADER, clean_web, effective, parse_recipients, valid_address, write_message, write_web_recipients
from .models import DailyMetric, Incident, MaintenanceWindow, User, UserPrefs

log = logging.getLogger("portal")
RECOVERY = timedelta(minutes=10)
KIND_SOURCE = {"mount_missing": "mounts", "smart_warn": "smart", "slow_speed": "speed", "backup_stale": "backup",
               "raid_degraded": "nas", "disk_high": "nas", "domain_expiring": "edge", "dns_drift": "edge",
               "app_down": "health", "cert_expiring": "cert"}
KEEP_DAYS = 30
OPT_OUT = {"danger": "alerts_off", "clear": "alerts_off", "digest": "report_off"}   # the test email goes to everyone
PREF = {"danger": "instant", "clear": "instant", "digest": "digest"}                 # the matching Profile switch (UserPrefs)
naive = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None)   # the DB stores naive UTC
aware = lambda d: d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def user_addresses(username: str, google_email: str | None) -> set:
    """How a portal user maps to a recipient: their linked Google email, and their username if it is itself an email
    address (so it matches a Settings > Alerts entry). A user with neither has no address and their prefs change nothing."""
    name = (username or "").strip().lower()
    return {a for a in ((google_email or "").strip().lower(), name) if valid_address(a)}


def _pref_off(db, field: str) -> set:
    """Addresses whose users switched `field` off on their Profile, minus any another user with that address still wants."""
    want, off = set(), set()
    for name, google, on in db.query(User.username, User.google_email, getattr(UserPrefs, field)).outerjoin(
            UserPrefs, UserPrefs.user_id == User.id):
        (want if on is not False else off).update(user_addresses(name, google))   # no row = the default (on)
    return off - want


class Recorder:
    def __init__(self, SessionLocal, outbox, to: str, tz: str = "America/New_York", now=lambda: datetime.now(timezone.utc),
                 mail_dir=None):
        self.SL, self.outbox, self.tz, self.now, self.mail_dir = SessionLocal, outbox, tz, now, mail_dir
        self.owners = parse_recipients(to)                         # alerts.env: always allowed, first = primary
        self.to = self.owners[0] if self.owners else ""           # the From/To on every message we write
        self.seen: dict = {}     # key -> consecutive checks present (not yet an incident)
        self.missing: dict = {}  # key -> consecutive checks absent (open incident)
        self.pruned_day = None

    # ----- addresses -----
    def _config(self) -> tuple:
        """(web-added addresses, owners switched off, chosen primary) from the settings table."""
        with self.SL() as db:
            web = clean_web(parse_recipients(settingsstore.get(db, "web_recipients")))
            off = tuple(a for a in parse_recipients(settingsstore.get(db, "owners_off")) if a in self.owners)
            return web, off, settingsstore.get(db, "mail_primary").strip().lower()

    def web_recipients(self) -> tuple:
        return self._config()[0]

    @property
    def recipients(self) -> tuple:
        """The allowlist, first = primary: owners (minus any removed here), then web-added addresses (the collector builds
        the same list from recipients.json)."""
        return effective(self.owners, *self._config())

    def notice_to(self) -> tuple:
        """Who hears about list changes: the owners still on the list, else whoever is primary."""
        cur = self.recipients
        return tuple(a for a in self.owners if a in cur) or cur[:1]

    def _save(self, db, web, off, primary) -> bool:
        for key, val in (("web_recipients", ",".join(web)), ("owners_off", ",".join(off)), ("mail_primary", primary)):
            settingsstore.put(db, key, val)
        try:   # the collector re-reads this file each send cycle (and validates it again)
            write_web_recipients(self.mail_dir / "recipients.json", web, off, primary)
            return True
        except OSError as e:
            log.warning("could not write the recipients file: %s", type(e).__name__)
            return False

    def add_web(self, raw: str, by: str) -> str:
        """Returns the Settings flash note. The file is written BEFORE the welcome is queued, so it can reach the new address."""
        if not self.mail_dir:
            return "address-off"
        addr = (raw or "").strip().lower()
        if not valid_address(addr):
            return "address-invalid"
        web, off, primary = self._config()
        if addr in self.recipients:
            return "address-dup"
        if addr in self.owners:   # an owner removed earlier comes back as an owner
            off = tuple(a for a in off if a != addr)
        elif len(web) >= MAX_WEB:
            return "address-limit"
        else:
            web = web + (addr,)
        with self.SL() as db:
            ok = self._save(db, web, off, primary)
        if not ok:
            return "address-failed"
        now = self.now()
        self._send(emails.recipient_welcome(self.to, now, self.tz), "notice", to=(addr,))
        self._send(emails.recipient_notice(self.to, addr, by, True, now, self.tz), "notice", to=self.notice_to())
        return "address-added"

    def remove_web(self, raw: str, by: str) -> str:
        """Removes any address on the list, owner or not. The last one can't go: somebody has to get the alerts."""
        addr = (raw or "").strip().lower()
        if not self.mail_dir:
            return "address-off"
        web, off, primary = self._config()
        cur = self.recipients
        if addr not in cur:
            return "address-gone"
        if len(cur) == 1:
            return "address-last"
        if addr in self.owners:
            off = off + (addr,)
        web = tuple(a for a in web if a != addr)
        if primary == addr:
            primary = ""
        with self.SL() as db:
            ok = self._save(db, web, off, primary)
            for key in ("alerts_off", "report_off"):
                settingsstore.put(db, key, ",".join(a for a in parse_recipients(settingsstore.get(db, key)) if a != addr))
        if not ok:
            return "address-failed"
        self._send(emails.recipient_notice(self.to, addr, by, False, self.now(), self.tz), "notice", to=self.notice_to())
        return "address-removed"

    def make_primary(self, raw: str, by: str) -> str:
        addr = (raw or "").strip().lower()
        if not self.mail_dir:
            return "address-off"
        if addr not in self.recipients:
            return "address-gone"
        web, off, _ = self._config()
        with self.SL() as db:
            ok = self._save(db, web, off, addr)
        return "primary-set" if ok else "address-failed"

    # ----- public -----
    def observe(self, snap: dict) -> None:
        now = self.now()
        out: list = []
        with self.SL() as db:
            st = settingsstore.get_all(db)
            self._incidents(db, snap, now, st, out)
            self._metrics(db, snap, now)
            db.commit()
        self._flush(out)
        out = []
        try:
            with self.SL() as db:
                self._digest(db, snap, now, settingsstore.get_all(db), out)
                db.commit()
        except Exception as e:  # noqa: BLE001 (a broken digest must not stop alerts)
            log.warning("morning report failed: %s", type(e).__name__)
            return
        self._flush(out)

    def trends(self) -> dict:
        with self.SL() as db:
            rows = db.query(DailyMetric).order_by(DailyMetric.day).all()
        out: dict = {}
        for r in rows:
            out.setdefault(r.key, []).append({"d": r.day, "v": r.value})
        return out

    def recent_incidents(self, hours: int = 24, limit: int = 50) -> list:
        since = naive(self.now() - timedelta(hours=hours))
        with self.SL() as db:
            rows = (db.query(Incident).filter((Incident.closed_at.is_(None)) | (Incident.closed_at >= since))
                    .order_by(Incident.opened_at.desc()).limit(limit).all())
            return [self._dict(i) for i in rows]

    def history(self, severity: str = "", state: str = "", page: int = 1, per: int = 25, prep=lambda rows: rows) -> tuple:
        """(rows for this page, total) of every incident, newest first. `prep` scrubs/drops rows BEFORE paging."""
        with self.SL() as db:
            q = db.query(Incident)
            if severity in ("crit", "warn"):
                q = q.filter(Incident.severity == severity)
            if state in ("open", "closed"):
                q = q.filter(Incident.closed_at.is_(None) if state == "open" else Incident.closed_at.is_not(None))
            rows = prep([self._dict(i) for i in q.order_by(Incident.opened_at.desc(), Incident.id.desc()).all()])
        return rows[(page - 1) * per:page * per], len(rows)

    # ----- Maintenance windows (any signed-in user) -----
    def add_window(self, target: str, start: datetime, end: datetime, note: str, by: str) -> int:
        with self.SL() as db:
            w = MaintenanceWindow(target=target, start=naive(start), end=naive(end), note=note, created_by=by)
            db.add(w)
            db.commit()
            return w.id

    def cancel_window(self, wid: int) -> bool:
        with self.SL() as db:
            w = db.get(MaintenanceWindow, wid)
            if w is None or w.cancelled:
                return False
            w.cancelled = True
            db.commit()
            return True

    def windows(self) -> list:
        """Every window, newest start first, with `state` active | upcoming | past (cancelled counts as past)."""
        now = naive(self.now())
        with self.SL() as db:
            rows = db.query(MaintenanceWindow).order_by(MaintenanceWindow.start.desc(), MaintenanceWindow.id.desc()).all()
            return [{"id": w.id, "target": w.target, "start": aware(w.start), "end": aware(w.end), "note": w.note,
                     "created_by": w.created_by, "cancelled": w.cancelled,
                     "state": "past" if w.cancelled or w.end <= now else "active" if w.start <= now else "upcoming"}
                    for w in rows]

    @staticmethod
    def _active(db, now: datetime) -> dict:
        """target -> latest end of the windows active right now."""
        out: dict = {}
        for t, end in db.query(MaintenanceWindow.target, MaintenanceWindow.end).filter(
                MaintenanceWindow.cancelled.is_(False), MaintenanceWindow.start <= naive(now), MaintenanceWindow.end > naive(now)):
            out[t] = max(out.get(t, end), end)
        return out

    def active_windows(self) -> dict:
        with self.SL() as db:
            return {t: aware(e) for t, e in self._active(db, self.now()).items()}

    @staticmethod
    def held_until(active: dict, key: str):
        """When the maintenance covering `key` ends (None = not in maintenance)."""
        ends = [e for t, e in active.items() if t in ("*", key)]
        return max(ends) if ends else None

    def mark_maintenance(self, incidents: list) -> list:
        """The digest's view: open incidents covered by an active window get `maint`."""
        active = self.active_windows()
        return [{**i, "maint": not i.get("closed_at") and self.held_until(active, i["key"]) is not None} for i in incidents]

    # ----- Acknowledge (a card's button; only OPEN incidents, so an alert in its first check has nothing to ack yet) -----
    def open_acks(self) -> dict:
        """key -> {acked_by, acked_at} for every open incident; a key missing here has no incident (no button)."""
        with self.SL() as db:
            return {i.key: {"acked_by": i.acked_by, "acked_at": aware(i.acked_at)}
                    for i in db.query(Incident).filter(Incident.closed_at.is_(None)).all()}

    def ack(self, key: str, by: str) -> bool:
        """False = no open incident with that key. Not yet emailed: mark it emailed without sending, so an unmute later
        doesn't send it. The ack stays on the row after it closes, which is what suppresses the all-clear."""
        with self.SL() as db:
            inc = db.query(Incident).filter(Incident.key == key, Incident.closed_at.is_(None)).first()
            if inc is None:
                return False
            if not inc.acked_by:   # conditional, so an email the recorder queued a moment ago isn't mistaken for a skipped one
                skipped = db.query(Incident).filter(Incident.id == inc.id, Incident.emailed.is_(False)).update(
                    {"emailed": True, "ack_skipped": True}, synchronize_session=False)
                inc.ack_skipped = bool(skipped)
            inc.acked_by, inc.acked_at = by, naive(self.now())
            db.commit()
            return True

    def unack(self, key: str) -> bool:
        """Undo sends nothing itself: `emailed` goes back to how the ack found it, so only an un-emailed open Danger
        (one the ack skipped) gets its email on the next check."""
        with self.SL() as db:
            inc = db.query(Incident).filter(Incident.key == key, Incident.closed_at.is_(None)).first()
            if inc is None:
                return False
            if inc.ack_skipped:
                inc.emailed = False
            inc.acked_by, inc.acked_at, inc.ack_skipped = None, None, False
            db.commit()
            return True

    def send_test(self) -> bool:
        if not self.to or not self.outbox:
            return False
        self._send(emails.test_message(self.to, self.now(), self.tz), "test")
        return True

    # ----- internals -----
    @staticmethod
    def _dict(i: Incident) -> dict:
        return {"key": i.key, "kind": i.kind, "target": i.target, "severity": i.severity, "message": i.message,
                "opened_at": aware(i.opened_at), "closed_at": aware(i.closed_at), "emailed": i.emailed,
                "acked_by": i.acked_by, "acked_at": aware(i.acked_at)}

    def _flush(self, out: list) -> None:
        for raw, prefix in out:
            self._send(raw, prefix)

    def picks(self, prefix: str) -> tuple:
        """The allowlisted addresses that want this kind of email (Settings switches them off per address)."""
        key = OPT_OUT.get(prefix)
        if not key:
            return self.recipients
        with self.SL() as db:
            off = set(parse_recipients(settingsstore.get(db, key)))
            unwanted = _pref_off(db, PREF[prefix])
        allowed = tuple(a for a in self.recipients if a not in off)
        out = tuple(a for a in allowed if a not in unwanted)
        if allowed and not out:   # Profile prefs only subtract, and never silently down to nobody
            if prefix == "digest":
                log.warning("every digest recipient switched the digest off in their profile; not sending it")
                return out
            log.warning("every recipient switched %s emails off in their profile; sending to the Settings list anyway", prefix)
            return allowed
        return out

    def _send(self, raw: bytes, prefix: str, to: tuple | None = None) -> None:
        """`to` (notices) ignores the Alerts/Report switches but still only goes to allowlisted addresses."""
        if not self.to or not self.outbox:
            return
        picks = tuple(a for a in self.recipients if a in to) if to is not None else self.picks(prefix)
        if not picks:
            return
        try:
            write_message(self.outbox, f"{PICK_HEADER}: {', '.join(picks)}\n".encode() + raw, prefix)
        except OSError as e:
            log.warning("could not queue an email: %s", type(e).__name__)

    def _alerts_on(self, st: dict, now: datetime) -> bool:
        if st.get("alerts_enabled") != "1":
            return False
        mute = st.get("mute_until") or ""
        try:
            return not (mute and datetime.fromisoformat(mute.replace("Z", "+00:00")) > now)
        except ValueError:
            return True

    @staticmethod
    def _in_doubt(inc: Incident, snap: dict) -> bool:
        """True when the check behind this incident isn't reporting, so its absence proves nothing."""
        if any(n.get("id") == inc.target and n.get("status") == "unknown" for n in snap.get("nodes") or []):
            return True
        src = KIND_SOURCE.get(inc.kind) or ("pings" if inc.kind == "host_down" and inc.target != "caddy" else None)
        return "live" in snap and src is not None and src not in snap["live"]

    def _incidents(self, db, snap: dict, now: datetime, st: dict, out: list) -> None:
        present = {f"{a['kind']}:{a['target']}": a for a in snap.get("alerts", []) if a.get("severity") in ("warn", "crit")}
        open_ = {i.key: i for i in db.query(Incident).filter(Incident.closed_at.is_(None)).all()}
        for key, a in present.items():
            self.missing.pop(key, None)
            inc = open_.get(key)
            if inc is None:   # back within the recovery window: the same incident, reopened quietly
                inc = (db.query(Incident).filter(Incident.key == key, Incident.clear_sent.is_(False),
                                                 Incident.closed_at >= naive(now - RECOVERY))
                       .order_by(Incident.closed_at.desc()).first())
                if inc is not None:
                    inc.closed_at = None
                    open_[key] = inc
                    self.seen.pop(key, None)
            if inc is not None:
                if a["severity"] == "crit" and inc.severity != "crit":   # Caution became Danger
                    inc.severity, inc.message = "crit", a["message"]
                    if inc.acked_by:   # an ack of the Caution doesn't cover the Danger: drop it, the Danger email goes out
                        inc.acked_by, inc.acked_at, inc.ack_skipped, inc.emailed = None, None, False, False
                continue
            self.seen[key] = self.seen.get(key, 0) + 1
            if self.seen[key] >= 2:
                self.seen.pop(key)
                db.add(Incident(key=key, kind=a["kind"], target=a["target"], severity=a["severity"], message=a["message"],
                                opened_at=naive(now)))
        for key in [k for k in self.seen if k not in present]:
            self.seen.pop(key)
        for key, inc in open_.items():
            if key in present:
                continue
            if self._in_doubt(inc, snap):
                self.missing.pop(key, None)
                continue
            self.missing[key] = self.missing.get(key, 0) + 1
            if self.missing[key] >= 2:
                self.missing.pop(key)
                inc.closed_at = naive(now)
        db.flush()
        on = self._alerts_on(st, now)
        active = self._active(db, now)
        held = lambda key: self.held_until(active, key) is not None
        if on:   # every open Danger not yet emailed: new ones, Caution→Danger, and ones that opened while muted
            for inc in db.query(Incident).filter(Incident.closed_at.is_(None), Incident.severity == "crit",
                                                 Incident.emailed.is_(False), Incident.acked_by.is_(None)).all():
                if held(inc.key):   # in maintenance: `emailed` stays False, so it goes out once the window ends
                    continue
                # claim it with a conditional UPDATE: an ack that lands between the read and here wins, so no email is queued
                if db.query(Incident).filter(Incident.id == inc.id, Incident.emailed.is_(False), Incident.acked_by.is_(None)
                                             ).update({"emailed": True}, synchronize_session=False):
                    out.append((emails.alert(self._dict(inc), self.to, now, self.tz), "danger"))
        for inc in db.query(Incident).filter(Incident.closed_at.is_not(None), Incident.clear_sent.is_(False),
                                             Incident.closed_at <= naive(now - RECOVERY)).all():
            due = inc.emailed and on and not inc.acked_by   # somebody acknowledged it: they know, no all-clear
            if due and held(inc.key):   # emailed before the window, closed inside it: the all-clear waits for the end
                continue
            inc.clear_sent = True
            # a held all-clear for a key with a NEW open incident would be a false all-clear: drop it
            if due and not db.query(Incident.id).filter(Incident.key == inc.key, Incident.closed_at.is_(None)).first():
                out.append((emails.alert(self._dict(inc), self.to, now, self.tz, cleared=True), "clear"))

    def _metrics(self, db, snap: dict, now: datetime) -> None:
        day = now.astimezone(ZoneInfo(self.tz)).strftime("%Y-%m-%d")
        for key, value in (snap.get("metrics") or {}).items():
            if value is None:
                continue
            row = db.get(DailyMetric, (day, key))
            if row:
                row.value = float(value)
            else:
                db.add(DailyMetric(day=day, key=key, value=float(value)))
        if self.pruned_day != day:
            self.pruned_day = day
            cutoff = (now.astimezone(ZoneInfo(self.tz)) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
            db.query(DailyMetric).filter(DailyMetric.day < cutoff).delete()

    def _digest(self, db, snap: dict, now: datetime, st: dict, out: list) -> None:
        if st.get("digest_enabled") != "1":
            return
        local = now.astimezone(ZoneInfo(self.tz))
        today = local.strftime("%Y-%m-%d")
        hh, mm = map(int, (st.get("digest_time") or "06:30").split(":"))
        if st.get("digest_last_sent") == today or (local.hour, local.minute) < (hh, mm):
            return
        out.append((emails.digest(snap, self.mark_maintenance(self.recent_incidents(24)), self.to, now, self.tz), "digest"))
        settingsstore.put(db, "digest_last_sent", today)
