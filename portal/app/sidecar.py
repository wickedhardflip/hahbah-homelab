"""The collector container: slow or secret-holding checks, each writing one JSON file the portal reads.

    python -m app.sidecar          (the `collector` service in hosts/central/compose.yml)

Jobs: nas (60 s, SSH with a key the NAS restricts to its stats script), mounts + pings (60 s), smart (daily 04:05) and
speed (daily 04:10), eero + eero_usage (5 min / hourly, the Eero cloud API: nodes, devices, usage, settings), edge (6 h, Porkbun + DNS via the Eero), backup (5 min, a copy of RePlexOn's database), and mailer
(30 s, sends the portal's outbox with msmtp to the owners + web-added addresses), and weather (15 min, Open-Meteo). One failing job never stops the others.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .registry import load_site

log = logging.getLogger("collector")


class Job:
    """Runs every `interval_s`, or once a day at `daily_at` ("HH:MM" local). A daily job also runs at start-up
    unless its last result is younger than `fresh_s` (so a deploy doesn't trigger an extra speed test)."""

    def __init__(self, name: str, interval_s: int, fn, daily_at: str | None = None, fresh_s: int | None = None,
                 timeout_s: float = 300, trigger: Path | None = None):
        self.trigger = trigger   # a file the portal touches to ask for a run now; newer than the last result = run
        self.name, self.interval_s, self.fn, self.next_at = name, interval_s, fn, 0.0
        self.timeout_s = timeout_s   # past this the job is recorded as failed (Timeout); it never overlaps itself
        self.daily_at, self.fresh_s, self.last_day = daily_at, fresh_s, None


class Runner:
    def __init__(self, out_dir: Path, jobs: list, now=lambda: datetime.now(timezone.utc), tz: str = "America/New_York"):
        self.out, self.jobs, self.now, self.tz = Path(out_dir), jobs, now, ZoneInfo(tz)

    def _age(self, name: str):
        try:
            doc = json.loads((self.out / f"{name}.json").read_text(encoding="utf-8"))
            if not doc.get("ok"):
                return None
            return (self.now() - datetime.fromisoformat(doc["checked_at"].replace("Z", "+00:00"))).total_seconds()
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _daily_due(self, job: Job) -> bool:
        local = self.now().astimezone(self.tz)
        hh, mm = map(int, job.daily_at.split(":"))
        slot_passed = (local.hour, local.minute) >= (hh, mm)
        if job.last_day is None:   # first look after start-up
            age = self._age(job.name)
            if age is None or age > (job.fresh_s or 0):
                return True
            job.last_day = local.date() if slot_passed else local.date() - timedelta(days=1)
            return False
        return local.date() != job.last_day and slot_passed

    def _write(self, name: str, doc: dict) -> None:
        tmp = self.out / f"{name}.json.tmp"
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, self.out / f"{name}.json")

    def _requested(self, job: Job) -> bool:
        """True when `job.trigger` is newer than the job's last result (ok or failed), so one click runs once."""
        try:
            asked = job.trigger.stat().st_mtime
        except (OSError, AttributeError):
            return False
        try:
            doc = json.loads((self.out / f"{job.name}.json").read_text(encoding="utf-8"))
            last = datetime.fromisoformat(doc["checked_at"].replace("Z", "+00:00")).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            last = 0.0
        return asked > last

    def _due(self, job: Job, mono: float) -> bool:
        """True (and the schedule advanced) when the job should run now."""
        if self._requested(job):   # an on-demand run leaves the daily schedule alone
            return True
        if job.daily_at:
            if not self._daily_due(job):
                return False
            local = self.now().astimezone(self.tz)
            hh, mm = map(int, job.daily_at.split(":"))
            # a start-up run before today's slot doesn't use the slot up
            job.last_day = local.date() if (local.hour, local.minute) >= (hh, mm) else local.date() - timedelta(days=1)
        elif mono < job.next_at:
            return False
        job.next_at = mono + job.interval_s
        return True

    def _record(self, job: Job, result: dict) -> None:
        doc = {"checked_at": self.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "interval_s": job.interval_s}
        try:
            self._write(job.name, {**doc, **result})
        except OSError as e:
            log.warning("collector could not write %s: %s", job.name, type(e).__name__)

    def _call(self, job: Job) -> dict:
        try:
            return {"ok": True, "data": job.fn()}
        except Exception as e:  # noqa: BLE001 (isolate jobs; only the class name is kept, never the message)
            log.warning("collector job %s failed: %s", job.name, type(e).__name__)
            return {"ok": False, "error": type(e).__name__}

    def _run(self, job: Job) -> None:
        r = self._call(job)
        self._record(job, {"ok": r["ok"], **{k: v for k, v in r.items() if k != "ok"}})

    def tick(self, mono: float) -> None:
        """One serial pass over every job (used by tests; `start` is what the collector runs)."""
        for job in self.jobs:
            if self._due(job, mono):
                self._run(job)

    def _loop(self, job: Job, stop: threading.Event, poll_s: float) -> None:
        worker = None
        while not stop.is_set():
            if (worker is None or not worker.is_alive()) and self._due(job, time.monotonic()):
                box: dict = {}
                def go(box=box):
                    box["r"] = self._call(job)
                worker = threading.Thread(target=go, name=f"job-{job.name}", daemon=True)
                worker.start()
                worker.join(job.timeout_s)
                if "r" in box:
                    self._record(job, box["r"])
                    worker = None
                else:
                    log.warning("collector job %s timed out after %ss", job.name, job.timeout_s)
                    self._record(job, {"ok": False, "error": "Timeout"})
                    # the stuck call can't be killed: this job waits for it, the other jobs carry on
            elif worker is not None and not worker.is_alive():
                worker = None
            stop.wait(poll_s)

    def start(self, poll_s: float = 1.0) -> threading.Event:
        """One thread per job, each on its own schedule: a slow or hung job never delays another."""
        stop = threading.Event()
        for job in self.jobs:
            threading.Thread(target=self._loop, args=(job, stop, poll_s), name=f"loop-{job.name}", daemon=True).start()
        return stop


def expected_names(apps_file: Path) -> list:
    site = load_site(apps_file)
    if not site["domain"]:
        return []
    ip = site["hosts"][site["edge_host"]]["ip"]
    home = {"name": f"home.{site['domain']}", "ip": ip, "host": site["edge_host"]}
    return [home] + [{"name": a["fqdn"], "ip": a["edge_ip"], "host": a["host"]} for a in site["apps"] if a.get("fqdn")]


def build_jobs(env=os.environ.get) -> list:
    from .collectors.appdbs import open_copy, replexon_summary
    from .collectors.edge import check_edge
    from .collectors.nas import NasSampler, run_nas_command

    jobs = []
    if env("NAS_HOST"):
        sampler = NasSampler()
        jobs.append(Job("nas", 60, lambda: sampler.sample(
            run_nas_command(env("NAS_HOST"), env("NAS_USER", "admin"), env("NAS_KEY", "/secrets/nas_collector"),
                            env("NAS_KNOWN_HOSTS", "/secrets/nas_known_hosts")), time.monotonic())))
    if env("EERO_SESSION"):
        from .collectors.eero import Eero
        eero = Eero(env("EERO_SESSION"))   # one object: both jobs share the cached network URL
        jobs.append(Job("eero", 300, eero.live))
        jobs.append(Job("eero_usage", 3600, eero.daily_usage))
        jobs.append(Job("eero_history", 86400, eero.history, daily_at="04:20", fresh_s=20 * 3600, timeout_s=120))
    if env("PORKBUN_API_KEY"):
        apps = Path(env("PORTAL_APPS_FILE", "/app/repo/apps.yaml"))
        jobs.append(Job("edge", 6 * 3600, lambda: check_edge(env("PORKBUN_API_KEY"), env("PORKBUN_API_SECRET_KEY"),
                                                               env("EDGE_DOMAIN", "hahbah.com"), expected_names(apps),
                                                               env("EERO_DNS", "192.168.4.1"))))
    def retry_once(fn):
        def run():
            try:
                return fn()
            except sqlite3.DatabaseError:   # copied in the middle of a checkpoint: copy again
                time.sleep(1)
                return fn()
        return run

    from .collectors.watch import check_mounts, ping_hosts, smart_all, speed_test
    from .mailer import send_outbox
    now = lambda: datetime.now(timezone.utc)
    if env("MOUNTS"):
        paths = [p for p in env("MOUNTS").split(",") if p]
        jobs.append(Job("mounts", 60, lambda: check_mounts(paths, env("MOUNTS_ROOT", "/host"), now())))
    if env("PING_TARGETS"):
        targets = dict(t.split("=", 1) for t in env("PING_TARGETS").split(",") if "=" in t)
        jobs.append(Job("pings", 60, lambda: ping_hosts(targets, now())))
    if env("NAS_HOST"):
        jobs.append(Job("smart", 86400, lambda: smart_all(run_nas_command(
            env("NAS_HOST"), env("NAS_USER", "admin"), env("NAS_KEY", "/secrets/nas_collector"),
            env("NAS_KNOWN_HOSTS", "/secrets/nas_known_hosts"), timeout=90, command="smart"), now()),
            daily_at="04:05", fresh_s=20 * 3600, timeout_s=600))
    if env("SPEEDTEST", "1") == "1":
        jobs.append(Job("speed", 86400, lambda: speed_test(now()), daily_at="04:10", fresh_s=20 * 3600,
                        timeout_s=900, trigger=Path(env("SPEED_REQUEST")) if env("SPEED_REQUEST") else None))
    if env("OUTBOX"):
        jobs.append(Job("mailer", 30, lambda: send_outbox(Path(env("OUTBOX")), env("ALERT_EMAIL_TO", ""),
                                                    recipients_file=env("RECIPIENTS_FILE"))))
    if env("REPLEXON_DB"):
        @retry_once
        def bk():
            with open_copy(Path(env("REPLEXON_DB"))) as c:
                return replexon_summary(c, env("PORTAL_TZ", "America/New_York"), datetime.now(timezone.utc))
        jobs.append(Job("backup", 300, bk))
    if env("SPORTS", "1") == "1":   # ballpark + arena + bus-stop cards; the job runs every 2 min but each API is hit every 15 (2 while live)
        from .collectors.sports import Sports
        from .collectors.sportsteams import read_picks
        jobs.append(Job("sports", 120, Sports(picks=lambda: read_picks(env("SPORTS_PICKS") or None))))   # the teams come from a file the portal writes (read-only here)
    if env("WEATHER_LOCATION"):   # Open-Meteo, no key; the place comes from a file the portal writes (read-only here)
        from .collectors.weather import fetch_weather, read_location
        jobs.append(Job("weather", 900, lambda: fetch_weather(read_location(env("WEATHER_LOCATION")), env("PORTAL_TZ", "America/New_York"))))
    return jobs


def main() -> None:
    os.umask(0o077)   # collector files, DB copies and the outbox are owner-only (the portal runs as the same uid)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    out = Path(os.environ.get("COLLECTOR_DIR", "/out"))
    runner = Runner(out, build_jobs(), tz=os.environ.get("PORTAL_TZ", "America/New_York"))
    log.info("collector started: %s", ", ".join(j.name for j in runner.jobs) or "no jobs configured")
    runner.start()
    threading.Event().wait()   # the job threads are daemons: keep the main thread alive


if __name__ == "__main__":
    main()
