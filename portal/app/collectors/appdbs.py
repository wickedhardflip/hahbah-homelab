"""Read-only summary of RePlexOn's SQLite database (a live app on central, in WAL mode).

A WAL database can't be opened from a read-only mount without writing a -shm file next to it, so open_copy()
copies the database and its -wal/-shm into a temp dir and opens the copy. They're small (about 14 MB together).
"""
import contextlib
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


@contextlib.contextmanager
def open_copy(db: Path):
    db = Path(db)
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal", "-shm"):
            src = db.with_name(db.name + suffix)
            if src.exists():
                shutil.copyfile(src, Path(tmp) / (db.name + suffix))
        conn = sqlite3.connect(Path(tmp) / db.name)
        try:
            yield conn
        finally:
            conn.close()


def _utc(naive: str) -> str:
    return naive.split(".")[0].replace(" ", "T") + "Z"


def _parse_utc(naive: str) -> datetime:
    return datetime.fromisoformat(naive.split(".")[0]).replace(tzinfo=timezone.utc)


def replexon_summary(c: sqlite3.Connection, tz: str, now: datetime) -> dict:
    """RePlexOn stores local wall-clock times; everything here is converted to UTC."""
    zone = ZoneInfo(tz)
    utc = lambda t: datetime.fromisoformat(t.split(".")[0]).replace(tzinfo=zone).astimezone(timezone.utc) if t else None
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None
    ok = c.execute("SELECT COALESCE(finished_at, started_at), duration_seconds, total_size_bytes FROM backup_runs WHERE backup_type = 'daily_mirror' "
                   "AND status = 'success' ORDER BY started_at DESC LIMIT 1").fetchone()
    last = c.execute("SELECT status, backup_type, finished_at, started_at, error_message FROM backup_runs "
                     "WHERE backup_type = 'daily_mirror' ORDER BY started_at DESC LIMIT 1").fetchone()
    out = {"last_success": None, "last_run": None, "hours_since_success": None}
    if ok:
        fin = utc(ok[0])
        out["last_success"] = {"finished_at": iso(fin), "duration_min": round((ok[1] or 0) / 60),
                               "size_gb": round((ok[2] or 0) / 1024 ** 3, 2)}
        out["hours_since_success"] = round((now - fin).total_seconds() / 3600, 1)
    if last:
        out["last_run"] = {"status": last[0], "type": last[1], "finished_at": iso(utc(last[2] or last[3])), "error": last[4]}
    detail = c.execute("SELECT transferred_bytes, files_transferred, db_safe FROM backup_runs WHERE backup_type = 'daily_mirror' "
                       "AND status = 'success' ORDER BY started_at DESC LIMIT 1").fetchone()
    if detail and out["last_success"]:
        out["last_success"]["changed_mb"] = round((detail[0] or 0) / 1024 ** 2, 1)
        out["last_success"]["changed_gb"] = round((detail[0] or 0) / 1024 ** 3, 2)
        out["last_success"]["files"] = detail[1]
        out["last_success"]["db_safe"] = None if detail[2] is None else bool(detail[2])   # Plex DB copied via sqlite .backup (True) or live rsync
    # distinct nights with a run / with a good run, last 30 days
    days = c.execute("SELECT COUNT(DISTINCT date(started_at)), COUNT(DISTINCT CASE WHEN status = 'success' THEN date(started_at) END) FROM backup_runs "
                     "WHERE backup_type = 'daily_mirror' AND started_at >= ?", ((now.astimezone(zone).date() - timedelta(days=30)).isoformat(),)).fetchone()
    out["rate_30d"] = {"good": days[1], "total": days[0]} if days[0] else None
    # the last 14 nights (home dates): ok / running / failed / missing
    local_now = now.astimezone(zone)
    end = local_now.date() if local_now.hour >= 6 else local_now.date() - timedelta(days=1)
    by_day: dict = {}
    for status, started in c.execute("SELECT status, started_at FROM backup_runs WHERE backup_type = 'daily_mirror' "
                                     "AND started_at >= ?", ((end - timedelta(days=14)).isoformat(),)):
        d = started[:10]
        if status == "success" or by_day.get(d) == "ok":
            by_day[d] = "ok"
        elif status == "running" or by_day.get(d) == "running":
            by_day[d] = "running"
        else:
            by_day[d] = "failed"
    out["nights"] = [{"day": (end - timedelta(days=i)).isoformat(),
                      "status": by_day.get((end - timedelta(days=i)).isoformat(), "missing")} for i in range(13, -1, -1)]
    # the 7 good runs before the latest one: the baseline a slow or odd-sized night is compared with
    prev = c.execute("SELECT duration_seconds, total_size_bytes FROM backup_runs WHERE backup_type = 'daily_mirror' AND status = 'success' "
                     "ORDER BY started_at DESC LIMIT 7 OFFSET 1").fetchall()
    prev = [p for p in prev if p[0] and p[1]]
    out["avg_7d"] = ({"duration_min": round(sum(p[0] for p in prev) / len(prev) / 60, 1),
                      "size_gb": round(sum(p[1] for p in prev) / len(prev) / 1024 ** 3, 2), "runs": len(prev)} if prev else None)
    settings = dict(c.execute("SELECT key, value FROM app_settings")) if c.execute(
        "SELECT name FROM sqlite_master WHERE name = 'app_settings'").fetchone() else {}
    try:
        snaps = sorted(s["date"] for s in json.loads(settings.get("snapshot_list") or "[]"))
        out["snapshots"] = {"count": len(snaps), "newest": snaps[-1], "oldest": snaps[0]} if snaps else {"count": 0}
    except (ValueError, KeyError, TypeError):
        out["snapshots"] = None
    reach = settings.get("nas_reachable")
    out["nas_reachable"] = None if reach is None else reach.lower() == "true"
    return out
