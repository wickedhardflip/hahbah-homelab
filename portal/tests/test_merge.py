import copy
import json
from datetime import datetime, timezone
from app import merge
from pathlib import Path

from app.merge import (apply_backup, apply_cert, apply_drives, apply_edge, apply_health, apply_mounts, apply_nas, apply_pings,
                       apply_speed, read_collector)

TOPO = json.loads((Path(__file__).parents[1] / "app/topology.json").read_text("utf-8"))
NOW = datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc)
TZ = "America/New_York"


def fresh():
    snap = copy.deepcopy(TOPO)
    return snap, {n["id"]: n for n in snap["nodes"]}, []


def doc(data, age_s=10, interval=60, ok=True):
    d = {"ok": ok, "checked_at": "x", "interval_s": interval, "age_s": age_s, "stale": age_s > 3 * interval}
    return {**d, "data": data} if ok else {**d, "error": "RuntimeError"}


def kinds(alerts):
    return sorted((a["kind"], a["severity"], a["target"]) for a in alerts)


# ---------- collector files ----------

def test_read_collector_marks_age_and_staleness(tmp_path):
    (tmp_path / "nas.json").write_text(json.dumps({"ok": True, "checked_at": "2026-10-02T00:29:00Z", "interval_s": 60, "data": {}}))
    (tmp_path / "edge.json").write_text(json.dumps({"ok": True, "checked_at": "2026-10-01T00:00:00Z", "interval_s": 21600, "data": {}}))
    (tmp_path / "junk.json").write_text("{not json")
    r = read_collector(tmp_path, NOW)
    assert r["nas"]["age_s"] == 60 and r["nas"]["stale"] is False
    assert r["edge"]["stale"] is True
    assert r["junk"] == {"ok": False, "error": "unreadable", "stale": True, "age_s": None}


def test_read_collector_without_a_directory_is_empty(tmp_path):
    assert read_collector(tmp_path / "missing", NOW) == {}


# ---------- certificate + Caddy ----------

def test_cert_updates_edge_and_alerts_by_days_left():
    for days, expect in ((60, []), (15, [("cert_expiring", "warn", "caddy")]), (5, [("cert_expiring", "crit", "caddy")])):
        snap, nodes, alerts = fresh()
        exp = datetime.fromtimestamp(NOW.timestamp() + days * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        apply_cert({"names": ["*.hahbah.com"], "issuer": "Let's Encrypt (YE1)", "issued": "2026-09-30T00:00:00Z", "expires": exp},
                   snap, nodes, alerts, NOW)
        assert snap["edge"]["cert"]["expires"] == exp and kinds(alerts) == expect and nodes["caddy"]["status"] == "good"


def test_caddy_unreachable_is_station_closed():
    snap, nodes, alerts = fresh()
    apply_cert({"error": "ConnectionRefusedError"}, snap, nodes, alerts, NOW)
    assert nodes["caddy"]["status"] == "crit" and kinds(alerts) == [("host_down", "crit", "caddy")]


# ---------- app health ----------

def test_health_sets_status_meta_and_alerts():
    snap, nodes, alerts = fresh()
    apply_health({"jobs": {"ok": True, "code": 200, "ms": 12.3}, "social": {"ok": True, "code": 200, "ms": 40.0},
                  "plex": {"ok": False, "code": None, "ms": 2500.0, "error": "TimeoutError"},
                  "ollama": {"ok": True, "code": 200, "ms": 5.0, "models": ["qwen2.5:3b", "llava:7b"]}}, snap, nodes, alerts)
    assert nodes["jobs"]["status"] == "good" and nodes["jobs"]["meta"]["Health check"] == "HTTP 200 · 12 ms"
    assert nodes["social"]["status"] == "good" and nodes["plex"]["status"] == "crit"
    assert nodes["ollama"]["meta"]["Models"] == "qwen2.5:3b, llava:7b" and nodes["pc"]["status"] == "good"
    assert kinds(alerts) == [("app_down", "crit", "plex")]
    assert not [a for a in snap["alerts"] if a["id"] in ("a2", "a3")]   # the "not monitored" notices are gone


def test_sleeping_pc_is_info_not_danger():
    snap, nodes, alerts = fresh()
    apply_health({"ollama": {"ok": False, "code": None, "ms": 2500.0, "error": "TimeoutError"}}, snap, nodes, alerts)
    assert nodes["ollama"]["status"] == "unknown" and nodes["pc"]["status"] == "unknown"
    assert kinds(alerts) == [("unmonitored", "info", "pc")]


# ---------- NAS ----------

NAS = {"stats": {"cpu_pct": 7, "load": [0.2, 0.1, 0.1], "cores": 2, "mem": {"total_gb": 7.72, "used_gb": 1.0, "cache_gb": 6.5, "avail_gb": 6.7},
                 "swap": {"total_gb": 6.63, "used_gb": 0.5}, "temps_c": [44, 42],
                 "disks": [{"mount": "/volume1", "size_gb": 10717.7, "used_gb": 6049.8, "pct": 57}],
                 "nics": [{"name": "eth1 (wired)", "rx_mbps": 0.1, "tx_mbps": 2.0, "rx_total_gb": 100.1, "tx_total_gb": 793.0}]},
       "uptime_s": 2105816, "raid": {"ok": True, "arrays": [{"name": "md2", "level": "raid5", "state": "UUUU", "ok": True}]}}


def test_nas_live_stats_and_raid():
    snap, nodes, alerts = fresh()
    apply_nas(doc(NAS), snap, nodes, alerts)
    n = nodes["nas"]
    assert n["status"] == "good" and n["stats"]["cpu_pct"] == 7 and n["stats"]["temps_c"] == [44, 42]
    assert n["meta"]["Uptime"] == "24 days 8 h" and n["meta"]["RAID"] == "RAID 5 · 4 of 4 drives in sync"
    assert alerts == []


def test_nas_first_sample_keeps_the_previous_cpu():
    snap, nodes, alerts = fresh()
    before = nodes["nas"]["stats"]["cpu_pct"]
    apply_nas(doc({**NAS, "stats": {**NAS["stats"], "cpu_pct": None}}), snap, nodes, alerts)
    assert nodes["nas"]["stats"]["cpu_pct"] == before


def test_nas_degraded_raid_and_full_disk():
    snap, nodes, alerts = fresh()
    bad = {**NAS, "raid": {"ok": False, "arrays": [{"name": "md2", "level": "raid5", "state": "UUU_", "ok": False}]},
           "stats": {**NAS["stats"], "disks": [{"mount": "/volume1", "size_gb": 1, "used_gb": 0.92, "pct": 92}]}}
    apply_nas(doc(bad), snap, nodes, alerts)
    assert kinds(alerts) == [("disk_high", "crit", "nas"), ("raid_degraded", "crit", "nas")]
    assert nodes["nas"]["meta"]["RAID"] == "RAID 5 · 3 of 4 drives in sync"


def test_nas_stale_or_failed_goes_unknown():
    for d in (doc(NAS, age_s=400), doc(None, ok=False)):
        snap, nodes, alerts = fresh()
        apply_nas(d, snap, nodes, alerts)
        assert nodes["nas"]["status"] == "unknown" and "Live stats" in nodes["nas"]["meta"]


# ---------- LegacyMonitor: mounts, drives, speed, pings ----------

WT = {"mounts": {p: {"mounted": True, "responsive": True, "checked_at": "2026-10-02T00:28:00Z"}
                 for p in ("/mnt/video", "/mnt/photo", "/mnt/music", "/mnt/audiobooks")},
      "drives": {d: {"model": "WD40PURX-64NZ6Y0", "temp_c": 28, "health": "healthy", "reallocated": 0, "power_on_hours": 20609,
                     "checked_at": "2026-10-01T04:05:35Z"} for d in ("sda", "sdb", "sdc", "sdd")},
      "speed": {"download_mbps": 203.0, "upload_mbps": 189.23, "latency_ms": 15.5, "tested_at": "2026-10-01T04:10:54Z", "median_7d_mbps": 190.0},
      "ping": {"router": {"reachable": True, "latency_ms": 8.13, "checked_at": "x"}, "nas": {"reachable": True, "latency_ms": 0.172, "checked_at": "x"}}}


def apply_all(wt, interval=300):
    snap, nodes, alerts = fresh()
    apply_mounts(doc(wt["mounts"], interval=60), snap, nodes, alerts, TZ)
    apply_drives(doc(wt["drives"], interval=86400), snap, nodes, alerts, TZ)
    apply_speed(doc(wt["speed"], interval=86400), snap, nodes, alerts, TZ, wt["speed"].get("median_7d_mbps"))
    apply_pings(doc(wt["ping"], interval=60), snap, nodes, alerts)
    return snap, nodes, alerts


def test_collector_fills_mounts_drives_speed_and_pings():
    snap, nodes, alerts = apply_all(WT)
    assert nodes["m_video"]["meta"]["Mounted"] == "Yes" and nodes["m_video"]["status"] == "good"
    assert nodes["sda"]["meta"]["SMART"] == "Healthy" and nodes["sda"]["meta"]["Temp"] == "28 °C"
    assert nodes["sda"]["meta"]["Power-on"] == "20,609 h (2.4 yrs)"
    assert nodes["isp"]["meta"]["Last speed test"] == "203.0 down / 189.2 up Mbps"
    assert nodes["isp"]["meta"]["Tested"] == "Oct 1, 12:10 AM"
    assert nodes["router"]["meta"]["Ping"] == "8.13 ms" and nodes["nas"]["meta"]["Ping"] == "0.17 ms"
    assert alerts == []


def test_collector_problems_raise_alerts():
    bad = copy.deepcopy(WT)
    bad["mounts"]["/mnt/music"] = {"mounted": True, "responsive": False, "checked_at": "x"}
    bad["drives"]["sdc"]["health"] = "failing"
    bad["speed"]["download_mbps"] = 40.0
    bad["ping"]["nas"]["reachable"] = False
    snap, nodes, alerts = apply_all(bad)
    assert kinds(alerts) == [("host_down", "crit", "nas"), ("mount_missing", "crit", "m_music"),
                             ("slow_speed", "warn", "isp"), ("smart_warn", "crit", "sdc")]
    assert nodes["m_music"]["status"] == "crit" and nodes["sdc"]["status"] == "crit"


# ---------- backups ----------

def test_backup_fresh_success():
    snap, nodes, alerts = fresh()
    apply_backup(doc({"last_success": {"finished_at": "2026-10-01T07:16:44Z", "duration_min": 17, "size_gb": 6.18},
                      "last_run": {"status": "success", "type": "daily_mirror", "finished_at": "2026-10-01T07:16:44Z", "error": None},
                      "hours_since_success": 17.2}, interval=300), snap, nodes, alerts, TZ)
    assert nodes["replexon"]["meta"]["Last backup"] == "Oct 1, 3:16 AM · 6.18 GB · 17 min"
    assert alerts == []


def test_backup_overdue_and_failed():
    for hours, last, expect in ((30, "success", []),
                                (40, "success", [("backup_stale", "crit", "replexon")]),
                                (20, "failure", [("backup_stale", "crit", "replexon")])):
        snap, nodes, alerts = fresh()
        apply_backup(doc({"last_success": {"finished_at": "2026-09-29T07:00:00Z", "duration_min": 15, "size_gb": 6.1},
                          "last_run": {"status": last, "type": "daily_mirror", "finished_at": "2026-10-01T07:01:00Z", "error": "rsync: refused"},
                          "hours_since_success": hours}, interval=300), snap, nodes, alerts, TZ)
        assert kinds(alerts) == expect


# ---------- edge: domain + DNS ----------

EDGE = {"domain": {"name": "hahbah.com", "registrar": "Porkbun", "created": "2026-09-30T17:32:45Z", "expires": "2027-09-30T17:32:45Z",
                   "auto_renew": True, "whois_privacy": True, "transfer_lock": True},
        "dns": {"expected": [{"name": "home.hahbah.com", "ip": "192.168.4.5", "host": "central"}], "wrong": [], "missing": [],
                "via_eero_ok": True, "via_eero_bad": []}}


def test_edge_live_domain_and_dns():
    snap, nodes, alerts = fresh()
    apply_edge(doc(EDGE, interval=21600), snap, nodes, alerts, NOW)
    assert snap["edge"]["domain"]["expires"] == "2027-09-30T17:32:45Z" and snap["edge"]["dns"]["via_eero_ok"] is True
    assert nodes["domain"]["meta"]["Renews"] == "Sep 30, 2027 · auto-renew on" and alerts == []


def test_edge_drift_and_lapsing_domain():
    snap, nodes, alerts = fresh()
    bad = copy.deepcopy(EDGE)
    bad["domain"]["auto_renew"] = False
    bad["dns"]["missing"] = ["jobs.hahbah.com"]
    bad["dns"]["via_eero_ok"], bad["dns"]["via_eero_bad"] = False, ["home.hahbah.com"]
    apply_edge(doc(bad, interval=21600), snap, nodes, alerts, NOW)
    assert kinds(alerts) == [("dns_drift", "warn", "domain"), ("domain_expiring", "warn", "domain")]
    assert snap["edge"]["dns"]["missing"] == ["jobs.hahbah.com"]


# ---------- review fixes: failures must turn stations grey, never leave Sep 30 green ----------

def test_failed_mount_and_smart_jobs_grey_their_stations():
    snap, nodes, alerts = fresh()
    apply_mounts(doc(None, ok=False, interval=60), snap, nodes, alerts, TZ)
    apply_drives(doc(None, ok=False, interval=86400), snap, nodes, alerts, TZ)
    assert nodes["m_video"]["status"] == "unknown" and nodes["sda"]["status"] == "unknown"
    assert "unavailable" in nodes["m_video"]["meta"]["Checked"]


def test_missing_collector_files_grey_their_stations():
    snap, nodes, alerts = fresh()
    apply_mounts(None, snap, nodes, alerts, TZ)
    apply_edge(None, snap, nodes, alerts, NOW)
    apply_backup(None, snap, nodes, alerts, TZ)
    assert nodes["m_music"]["status"] == "unknown" and nodes["domain"]["status"] == "unknown"
    assert nodes["replexon"]["meta"]["Last backup"] == "not collected yet"


def test_failed_edge_job_greys_the_domain():
    snap, nodes, alerts = fresh()
    apply_edge(doc(None, ok=False, interval=21600), snap, nodes, alerts, NOW)
    assert nodes["domain"]["status"] == "unknown" and "unavailable" in nodes["domain"]["meta"]["Live check"]


def test_read_collector_survives_an_odd_timestamp(tmp_path):
    (tmp_path / "nas.json").write_text(json.dumps({"ok": True, "checked_at": 123, "interval_s": 60, "data": {}}))
    (tmp_path / "edge.json").write_text(json.dumps({"ok": True, "checked_at": "2026-10-02T00:29:00Z", "interval_s": 21600, "data": {}}))
    r = read_collector(tmp_path, NOW)
    assert r["nas"]["error"] == "unreadable" and r["edge"]["stale"] is False


def test_backup_alert_does_not_show_raw_error_text():
    snap, nodes, alerts = fresh()
    apply_backup(doc({"last_success": {"finished_at": "2026-10-01T07:16:44Z", "duration_min": 17, "size_gb": 6.18},
                      "last_run": {"status": "failure", "type": "daily_mirror", "finished_at": "2026-10-02T07:01:00Z",
                                   "error": "rsync: /var/snap/secret/path refused"}, "hours_since_success": 17.0}, interval=300),
                 snap, nodes, alerts, TZ)
    assert alerts and "/var/snap" not in alerts[0]["message"]


def test_backup_slow_or_odd_sized_night_is_caution():
    base = {"last_success": {"finished_at": "2026-10-01T07:16:44Z", "duration_min": 40, "size_gb": 9.0},
            "last_run": {"status": "success", "type": "daily_mirror", "finished_at": "2026-10-01T07:16:44Z", "error": None},
            "hours_since_success": 17.2, "avg_7d": {"duration_min": 14.0, "size_gb": 6.18, "runs": 7},
            "nights": [{"day": "2026-10-01", "status": "ok"}], "snapshots": {"count": 2, "newest": "2026-09-27", "oldest": "2026-09-20"},
            "nas_reachable": True}
    snap, nodes, alerts = fresh()
    apply_backup(doc(base, interval=300), snap, nodes, alerts, TZ)
    assert kinds(alerts) == [("backup_stale", "warn", "replexon"), ("backup_stale", "warn", "replexon")]
    assert nodes["replexon"]["meta"]["Last 14 nights"] == "1 of 1 OK" and nodes["replexon"]["meta"]["Backup target"] == "NAS reachable"


def test_a_source_that_stopped_reporting_raises_its_own_alert():
    files = {"mounts": {"ok": True, "stale": True, "age_s": 3600},      # collector stopped writing an hour ago
             "pings": {"ok": True, "stale": False, "age_s": 20},
             "speed": {"ok": False, "error": "timeout", "stale": False, "age_s": 60},
             "nas": {"ok": True, "stale": True, "age_s": 200}}         # only just stale: not yet
    alerts = []
    merge.apply_sources(files, ["mounts", "pings", "speed", "nas", "backup"], alerts)
    got = {(a["target"], a["severity"]) for a in alerts}
    assert got == {("mounts", "crit"), ("speed", "warn"), ("backup", "crit")}   # backup: no file at all
    assert all(a["kind"] == "source_down" for a in alerts)
