import sys
import json
from datetime import datetime, timezone
from pathlib import Path

from app.collectors.watch import check_mounts, parse_ping, parse_smart, parse_speedtest, ping_hosts, split_drives

FX = Path(__file__).parent / "fixtures/collector"
NOW = datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)


def test_parse_smart_reads_health_temp_and_counters():
    d = parse_smart((FX / "synodisk_sda.txt").read_text(encoding="utf-8"))
    assert d["model"].startswith("WD") and d["health"] == "healthy"
    assert isinstance(d["temp_c"], int) and 15 < d["temp_c"] < 70
    assert d["reallocated"] == 0 and d["power_on_hours"] > 20000


def test_failing_attribute_marks_the_drive_failing():
    raw = (FX / "synodisk_sda.txt").read_text(encoding="utf-8")
    bad = raw.replace("Id: 5\nCurrent: 200\nWorst: 200\nThreshold: 140\nRaw: 0\nStatus: OK", "Id: 5\nCurrent: 100\nWorst: 100\nThreshold: 140\nRaw: 12\nStatus: FAILING_NOW")
    assert parse_smart(bad)["health"] == "failing"


def test_split_drives_from_the_nas_smart_command():
    one = (FX / "synodisk_sda.txt").read_text(encoding="utf-8")
    out = "".join(f"==drive {n}==\n{one}\n" for n in ("sda", "sdb")) + "==end==\n"
    assert list(split_drives(out)) == ["sda", "sdb"]


def test_parse_ping():
    out = "PING 192.168.4.4 (192.168.4.4) 56(84) bytes of data.\n64 bytes from 192.168.4.4: icmp_seq=1 ttl=64 time=0.172 ms\n"
    assert parse_ping(0, out) == {"reachable": True, "latency_ms": 0.172}
    assert parse_ping(1, "1 packets transmitted, 0 received") == {"reachable": False, "latency_ms": None}


def test_ping_hosts_runs_each_target():
    calls = []
    def run(argv, timeout):
        calls.append(argv[-1])
        return (0, "64 bytes from x: icmp_seq=1 ttl=64 time=5.12 ms") if argv[-1] != "10.0.0.9" else (1, "")
    r = ping_hosts({"router": "192.168.4.1", "nas": "10.0.0.9"}, NOW, run=run)
    assert r["router"]["latency_ms"] == 5.12 and r["nas"]["reachable"] is False and r["nas"]["checked_at"] == "2026-10-02T05:00:00Z"
    assert calls == ["192.168.4.1", "10.0.0.9"]


def test_parse_speedtest_json():
    j = json.dumps({"download": 203_000_000.0, "upload": 189_230_000.0, "ping": 15.468, "timestamp": "2026-10-01T04:10:54.369567Z"})
    assert parse_speedtest(j, NOW) == {"download_mbps": 203.0, "upload_mbps": 189.2, "latency_ms": 15.5, "tested_at": "2026-10-02T05:00:00Z"}


def test_check_mounts_marks_missing_and_hung(tmp_path):
    (tmp_path / "mnt").mkdir()
    calls = []
    def probe(argv, timeout):          # the whole check runs in one child: 0 ok, 3 not mounted, anything else hung
        calls.append((argv, timeout))
        return {"video": 0, "music": 124, "photo": 3}[argv[-1].rsplit("/", 1)[-1]]
    r = check_mounts(["/mnt/video", "/mnt/music", "/mnt/photo"], str(tmp_path), NOW, run=probe)
    assert r["/mnt/video"] == {"mounted": True, "responsive": True, "checked_at": "2026-10-02T05:00:00Z"}
    assert r["/mnt/music"] == {"mounted": True, "responsive": False, "checked_at": "2026-10-02T05:00:00Z"}
    assert r["/mnt/photo"]["mounted"] is False and r["/mnt/photo"]["responsive"] is False
    assert all(t <= 10 for _, t in calls)


def test_check_mounts_refuses_a_wrong_root(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):    # a typo in MOUNTS_ROOT must fail the job, not report every mount missing
        check_mounts(["/mnt/video"], str(tmp_path / "nope"), NOW, run=lambda a, t: 0)


@__import__("pytest").mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_detached_probe_gives_up_on_a_child_that_hangs():
    import time
    from app.collectors.watch import run_detached
    t0 = time.monotonic()
    assert run_detached(["sh", "-c", "sleep 30 & sleep 30"], 0.5) == 124   # grandchild holds nothing we wait on
    assert time.monotonic() - t0 < 3
