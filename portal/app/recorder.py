"""After every 30 s snapshot: open/close incidents, email Danger + all-clears, keep the daily numbers, send the digest.

An incident opens when a Caution or Danger alert shows in 2 checks in a row and closes after 2 checks without it, so a
single blip is never an incident. Only Danger is emailed. If it comes back within RECOVERY of closing, the same incident
reopens quietly; the all-clear goes out only after RECOVERY with no relapse, so the last email is never a false "all clear".
An incident whose source stopped reporting (station grey, or its collector file stale) stays open: no news is not good news.
Emails are queued only after the database commit, so a failed save never resends them.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import emails, settingsstore
from .mailer import MAX_WEB, PICK_HEADER, clean_web, effective, parse_recipients, valid_address, write_message, write_web_recipients
from .models import DailyMetric, Incident

log = logging.getLogger("portal")
RECOVERY = timedelta(minutes=10)
KIND_SOURCE = {"mount_missing": "mounts", "smart_warn": "smart", "slow_speed": "speed", "backup_stale": "backup",
               "raid_degraded": "nas", "disk_high": "nas", "domain_expiring": "edge", "dns_drift": "edge",
               "app_down": "health", "cert_expiring": "cert"}
KEEP_DAYS = 30
OPT_OUT = {"danger": "alerts_off", "clear": "alerts_off", "digest": "report_off"}   # the test email goes to everyone
naive = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None)   # the DB stores naive UTC
aware = lambda d: d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


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

    def send_test(self) -> bool:
        if not self.to or not self.outbox:
            return False
        self._send(emails.test_message(self.to, self.now(), self.tz), "test")
        return True

    # ----- internals -----
    @staticmethod
    def _dict(i: Incident) -> dict:
        return {"key": i.key, "kind": i.kind, "target": i.target, "severity": i.severity, "message": i.message,
                "opened_at": aware(i.opened_at), "closed_at": aware(i.closed_at), "emailed": i.emailed}

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
        return tuple(a for a in self.recipients if a not in off)

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
        if on:   # every open Danger not yet emailed: new ones, Caution→Danger, and ones that opened while muted
            for inc in db.query(Incident).filter(Incident.closed_at.is_(None), Incident.severity == "crit",
                                                 Incident.emailed.is_(False)).all():
                inc.emailed = True
                out.append((emails.alert(self._dict(inc), self.to, now, self.tz), "danger"))
        for inc in db.query(Incident).filter(Incident.closed_at.is_not(None), Incident.clear_sent.is_(False),
                                             Incident.closed_at <= naive(now - RECOVERY)).all():
            inc.clear_sent = True
            if inc.emailed and on:
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
        out.append((emails.digest(snap, self.recent_incidents(24), self.to, now, self.tz), "digest"))
        settingsstore.put(db, "digest_last_sent", today)
