import json
from datetime import datetime, timezone

from app.sidecar import Job, Runner, expected_names

NOW = datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc)


def test_each_job_writes_its_own_file(tmp_path):
    r = Runner(tmp_path, [Job("a", 60, lambda: {"x": 1}), Job("b", 60, lambda: {"y": 2})], now=lambda: NOW)
    r.tick(0.0)
    a = json.loads((tmp_path / "a.json").read_text())
    assert a == {"ok": True, "checked_at": "2026-10-02T00:30:00Z", "interval_s": 60, "data": {"x": 1}}
    assert json.loads((tmp_path / "b.json").read_text())["data"] == {"y": 2}
    assert not list(tmp_path.glob("*.tmp"))


def test_a_failing_job_writes_ok_false_and_the_others_still_run(tmp_path):
    def boom():
        raise RuntimeError("ssh exit 255: secret-looking stderr")
    r = Runner(tmp_path, [Job("bad", 60, boom), Job("good", 60, lambda: {"z": 3})], now=lambda: NOW)
    r.tick(0.0)
    bad = json.loads((tmp_path / "bad.json").read_text())
    assert bad["ok"] is False and bad["error"] == "RuntimeError" and "data" not in bad
    assert json.loads((tmp_path / "good.json").read_text())["ok"] is True


def test_jobs_run_on_their_own_interval(tmp_path):
    calls = {"fast": 0, "slow": 0}
    def counter(name):
        def f():
            calls[name] += 1
            return {}
        return f
    r = Runner(tmp_path, [Job("fast", 60, counter("fast")), Job("slow", 300, counter("slow"))], now=lambda: NOW)
    for t in (0, 30, 60, 120, 299, 300):
        r.tick(float(t))
    assert calls == {"fast": 4, "slow": 2}


def test_expected_names_come_from_apps_yaml(tmp_path):
    p = tmp_path / "apps.yaml"
    p.write_text("domain: hahbah.com\nedge_host: central\nhosts:\n  central: {ip: 192.168.4.5}\napps:\n"
                 "  - {id: jobs, name: J, subdomain: jobs, host: central, port: 8080, health: /, lan_url: 'http://x'}\n")
    assert expected_names(p) == [{"name": "home.hahbah.com", "ip": "192.168.4.5", "host": "central"},
                                 {"name": "jobs.hahbah.com", "ip": "192.168.4.5", "host": "central"}]


def test_a_write_failure_does_not_crash_the_loop(tmp_path):
    out = tmp_path / "not-a-dir"
    out.write_text("x")   # writing <out>/a.json.tmp will fail
    Runner(out, [Job("a", 60, lambda: {})], now=lambda: NOW).tick(0.0)


def test_daily_job_runs_at_its_time_and_not_on_every_restart(tmp_path):
    from datetime import timedelta
    clock = {"now": datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)}   # 3:00 AM in New York
    calls = []
    def make():
        return Runner(tmp_path, [Job("speed", 86400, lambda: calls.append(1) or {}, daily_at="04:10", fresh_s=20 * 3600)],
                      now=lambda: clock["now"], tz="America/New_York")
    r = make()
    r.tick(0.0)                                   # no file yet: runs once at start
    assert calls == [1]
    clock["now"] += timedelta(minutes=30)
    r.tick(1800.0)                                # 3:30 AM: not time yet
    assert calls == [1]
    clock["now"] = datetime(2026, 10, 2, 8, 11, tzinfo=timezone.utc)   # 4:11 AM
    r.tick(4000.0)
    assert calls == [1, 1]
    r.tick(4100.0)                                # same day: no second run
    assert calls == [1, 1]
    r2 = make()                                   # a deploy restarts the collector: the file is fresh, so no extra speed test
    r2.tick(0.0)
    assert calls == [1, 1]
    clock["now"] = datetime(2026, 10, 3, 8, 15, tzinfo=timezone.utc)   # next day 4:15 AM
    r2.tick(10.0)
    assert calls == [1, 1, 1]


def _wait(cond, timeout=5.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_hanging_job_does_not_delay_another(tmp_path):
    import threading
    release = threading.Event()
    hang = Job("hang", 60, lambda: release.wait(30) and {}, timeout_s=0.3)
    fast = Job("fast", 60, lambda: {"v": 1})
    r = Runner(tmp_path, [hang, fast], now=lambda: NOW)
    stop = r.start(poll_s=0.02)
    try:
        assert _wait(lambda: (tmp_path / "fast.json").exists())
        assert json.loads((tmp_path / "fast.json").read_text())["data"] == {"v": 1}
        assert _wait(lambda: (tmp_path / "hang.json").exists())   # timed out, recorded as failed
        assert json.loads((tmp_path / "hang.json").read_text())["error"] == "Timeout"
    finally:
        stop.set()
        release.set()


def test_a_job_never_overlaps_itself_and_a_raising_job_is_isolated(tmp_path):
    import threading
    import time
    active, peak, runs = [0], [0], [0]
    def slow():
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        runs[0] += 1
        time.sleep(0.25)
        active[0] -= 1
        return {}
    def boom():
        raise RuntimeError("secret")
    r = Runner(tmp_path, [Job("slow", 0, slow, timeout_s=0.05), Job("bad", 0, boom)], now=lambda: NOW)
    stop = r.start(poll_s=0.02)
    try:
        assert _wait(lambda: runs[0] >= 2)
        assert json.loads((tmp_path / "bad.json").read_text())["error"] == "RuntimeError"
    finally:
        stop.set()
    assert peak[0] == 1
