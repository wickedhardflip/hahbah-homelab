import json
import sqlite3
from datetime import datetime, timezone

from app.collectors.appdbs import open_copy, replexon_summary

NOW = datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc)   # 8:30 PM Oct 1 in New York



def replexon_db(tmp_path, runs):
    path = tmp_path / "replexon.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE backup_runs (id INTEGER PRIMARY KEY, backup_type VARCHAR(20), status VARCHAR(10), started_at DATETIME,"
              " finished_at DATETIME, duration_seconds FLOAT, total_size_bytes INTEGER, transferred_bytes INTEGER,"
              " files_transferred INTEGER, error_message TEXT, raw_log TEXT, triggered_by VARCHAR(10), email_sent BOOLEAN, db_safe BOOLEAN)")
    c.executemany("INSERT INTO backup_runs (backup_type,status,started_at,finished_at,duration_seconds,total_size_bytes,error_message,"
                  "triggered_by,email_sent) VALUES (?,?,?,?,?,?,?,'timer',0)", runs)
    c.commit()
    c.close()
    return path


def test_replexon_last_success_and_last_run(tmp_path):
    db = replexon_db(tmp_path, [("daily_mirror", "success", "2026-09-30 07:00:00", "2026-09-30 07:13:50", 830.0, 6635094229, None),
                                ("snapshot", "success", "2026-09-27 04:00:00", "2026-09-27 04:02:00", 120.0, 100, None),
                                ("daily_mirror", "success", "2026-10-01 07:00:00", "2026-10-01 07:16:44", 1004.0, 6635393489, None)])
    with open_copy(db) as c:
        r = replexon_summary(c, "America/New_York", NOW)
    assert r["last_success"]["finished_at"] == "2026-10-01T07:16:44Z"   # 3:16 AM EDT
    assert r["last_success"]["size_gb"] == 6.18 and r["last_success"]["duration_min"] == 17
    assert r["last_run"]["status"] == "success" and r["hours_since_success"] == 17.2


def test_replexon_failed_last_run(tmp_path):
    db = replexon_db(tmp_path, [("daily_mirror", "success", "2026-09-29 07:00:00", "2026-09-29 07:14:27", 867.0, 1, None),
                                ("daily_mirror", "failure", "2026-10-01 07:00:00", "2026-10-01 07:01:00", 60.0, None, "rsync: connection refused")])
    with open_copy(db) as c:
        r = replexon_summary(c, "America/New_York", NOW)
    assert r["last_run"] == {"status": "failure", "type": "daily_mirror", "finished_at": "2026-10-01T07:01:00Z",
                             "error": "rsync: connection refused"}
    assert r["hours_since_success"] == 65.3


def test_open_copy_reads_a_wal_database_without_writing_next_to_it(tmp_path):
    path = tmp_path / "w.db"
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (42)")
    c.commit()   # left un-checkpointed in the -wal file, like a live app's database
    before = sorted(p.name for p in tmp_path.iterdir())
    with open_copy(path) as ro:
        assert ro.execute("SELECT x FROM t").fetchone() == (42,)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    c.close()


def test_backup_without_a_finish_time_uses_the_start(tmp_path):
    db = replexon_db(tmp_path, [("daily_mirror", "success", "2026-10-01 07:00:00", None, None, 6635393489, None)])
    with open_copy(db) as c:
        r = replexon_summary(c, "America/New_York", NOW)
    assert r["last_success"]["finished_at"] == "2026-10-01T07:00:00Z" and r["hours_since_success"] == 17.5


def replexon_full(tmp_path, runs, snapshots=None, reachable="true"):
    db = replexon_db(tmp_path, runs)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE app_settings (key VARCHAR(100) PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME NOT NULL)")
    c.executemany("INSERT INTO app_settings VALUES (?,?,'x')", [
        ("snapshot_list", json.dumps(snapshots or [{"date": "2026-09-27", "age_days": 4}, {"date": "2026-09-20", "age_days": 11}])),
        ("nas_reachable", reachable), ("nas_last_check", "2026-10-02T00:25:29+00:00")])
    c.commit(); c.close()
    return db


def nightly(day, status="success", minutes=14.0, size=6635393489):
    return ("daily_mirror", status, f"2026-{day} 07:00:00", f"2026-{day} 07:{int(minutes):02d}:00", minutes * 60, size, None if status == "success" else "rsync failed")


def test_backup_history_snapshots_and_averages(tmp_path):
    runs = [nightly(f"09-{d:02d}") for d in range(20, 30)] + [nightly("09-30", "failure"), nightly("10-01", minutes=17)]
    with open_copy(replexon_full(tmp_path, runs)) as c:
        r = replexon_summary(c, "America/New_York", NOW)
    nights = r["nights"]
    assert len(nights) == 14 and nights[-1] == {"day": "2026-10-01", "status": "ok"}
    assert nights[-2]["status"] == "failed" and nights[0] == {"day": "2026-09-18", "status": "missing"}
    assert r["snapshots"] == {"count": 2, "newest": "2026-09-27", "oldest": "2026-09-20"}
    assert r["nas_reachable"] is True
    assert r["avg_7d"]["duration_min"] == 14.0 and r["avg_7d"]["size_gb"] == 6.18   # the 7 good runs before the latest


def test_a_backup_still_running_is_not_a_failed_night(tmp_path):
    runs = [nightly("09-30"), ("daily_mirror", "running", "2026-10-01 07:00:00", None, None, None, None)]
    with open_copy(replexon_full(tmp_path, runs)) as c:
        r = replexon_summary(c, "America/New_York", NOW)
    assert r["nights"][-1] == {"day": "2026-10-01", "status": "running"}


def test_replexon_transfer_db_safe_and_30d_rate(tmp_path):
    db = replexon_db(tmp_path, [("daily_mirror", "success", "2026-09-29 07:00:00", "2026-09-29 07:10:00", 600.0, 100, None),
                                ("daily_mirror", "failure", "2026-09-30 07:00:00", "2026-09-30 07:01:00", 60.0, None, "x"),
                                ("daily_mirror", "success", "2026-10-01 07:00:00", "2026-10-01 07:16:44", 1004.0, 6635393489, None)])
    c = sqlite3.connect(db)
    c.execute("UPDATE backup_runs SET transferred_bytes = 2147483648, files_transferred = 321, db_safe = 1 WHERE started_at LIKE '2026-10-01%'")
    c.commit()
    c.close()
    with open_copy(db) as c:
        r = replexon_summary(c, "America/New_York", NOW)
    ok = r["last_success"]
    assert ok["changed_gb"] == 2.0 and ok["files"] == 321 and ok["db_safe"] is True
    assert r["rate_30d"] == {"good": 2, "total": 3}


def test_replexon_db_safe_unrecorded_is_none(tmp_path):
    db = replexon_db(tmp_path, [("daily_mirror", "success", "2026-10-01 07:00:00", "2026-10-01 07:16:44", 1004.0, 5, None)])
    with open_copy(db) as c:
        r = replexon_summary(c, "America/New_York", NOW)
    assert r["last_success"]["db_safe"] is None


def test_replexon_2_stores_utc_so_a_3am_eastern_run_reports_3am(tmp_path):
    db = replexon_db(tmp_path, [("daily_mirror", "success", "2026-10-01 07:00:46.450057", "2026-10-01 07:01:46.452120", 60.0, 4049150297, None)])
    with open_copy(db) as c:
        r = replexon_summary(c, "America/New_York", NOW)
    assert r["last_success"]["finished_at"] == "2026-10-01T07:01:46Z"   # not 11:01Z (the old double conversion)
    assert r["nights"][-1] == {"day": "2026-10-01", "status": "ok"}
