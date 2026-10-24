"""Smart plugs: settings validation, the Wyze wrapper, command files, the schedule engine. A fake client; no network."""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from app.collectors.wyze import Backoff, Wyze
from app.plugs import PlugService, next_event, read_config, validate_plug_config, write_command, write_json
from app.sidecar import build_jobs

SCHED = {"id": "s1", "days": [0, 1, 2, 3, 4], "time": "17:30", "action": "on", "enabled": True}
GOOD = {"label": "Hallway lamp", "schedules_enabled": True, "schedules": [SCHED]}


# ---------- validation ----------

@pytest.mark.parametrize("change", [
    {"days": [7]}, {"days": []}, {"days": [1, 1]}, {"days": [True]}, {"days": "1"},
    {"time": "24:00"}, {"time": "9:30"}, {"time": "17:60"}, {"time": 1730},
    {"action": "toggle"}, {"action": None}, {"id": ""}, {"id": "a b"}, {"enabled": "yes"}, {"extra": 1}])
def test_bad_schedule_is_refused(change):
    with pytest.raises(ValueError):
        validate_plug_config({**GOOD, "schedules": [{**SCHED, **change}]})


@pytest.mark.parametrize("bad", [
    {"label": "x" * 41}, {"label": 5}, {"label": "tab\there"}, {"schedules_enabled": "true"}, {"schedules": "no"},
    {"schedules": [SCHED] * 2}, {"schedules": [{**SCHED, "id": f"s{i}"} for i in range(21)]}, {"nope": 1}])
def test_bad_plug_config_is_refused(bad):
    with pytest.raises(ValueError):
        validate_plug_config({**GOOD, **bad})


def test_good_config_is_cleaned():
    out = validate_plug_config({"label": "  Lamp ", "schedules": [{**SCHED, "days": [4, 0]}]})
    assert out == {"label": "Lamp", "schedules_enabled": False, "schedules": [{**SCHED, "days": [0, 4]}]}
    assert validate_plug_config({"schedules": [{**SCHED, "id": f"s{i}"} for i in range(20)]})["schedules"][19]["id"] == "s19"


def test_read_config_ignores_bad_entries_and_bad_files(tmp_path):
    f = tmp_path / "config.json"
    assert read_config(f) == {}
    f.write_text("{not json")
    assert read_config(f) == {}
    f.write_text(json.dumps({"plugs": {"a": GOOD, "b": {**GOOD, "label": "x" * 99}, "c": 5}}))
    assert list(read_config(f)) == ["a"]


# ---------- next event ----------

def test_next_event():
    mon = datetime(2026, 10, 26, 12, 0)   # a Monday
    assert next_event(GOOD, mon) == "Mon 17:30 on"
    assert next_event(GOOD, mon.replace(hour=18)) == "Tue 17:30 on"
    assert next_event(GOOD, datetime(2026, 10, 30, 18, 0)) == "Mon 17:30 on"   # Friday evening: next is Monday
    assert next_event({**GOOD, "schedules_enabled": False}, mon) is None
    assert next_event({**GOOD, "schedules": [{**SCHED, "enabled": False}]}, mon) is None
    two = {**GOOD, "schedules": [SCHED, {**SCHED, "id": "s2", "days": [0], "time": "13:00", "action": "off"}]}
    assert next_event(two, mon) == "Mon 13:00 off"
    assert next_event(GOOD, mon.replace(hour=17, minute=30)) == "Tue 17:30 on"   # exactly now is already past


# ---------- the Wyze wrapper ----------

class FakeSdk:
    """Stands in for wyze_sdk.Client: records calls, can be told to fail."""

    def __init__(self, plugs):
        self.calls, self.fail = [], None
        self.plugs = NS(list=self.list, info=self.info, turn_on=lambda **k: self.cmd("on", k), turn_off=lambda **k: self.cmd("off", k),
                        set_away_mode=lambda **k: self.cmd("away_on" if k["away_mode"] else "away_off", k))
        self.items = plugs

    def _go(self, what):
        self.calls.append(what)
        if self.fail:
            raise self.fail

    def list(self):
        self._go("list")
        return self.items

    def info(self, device_mac):
        self._go("info")
        return next((p for p in self.items if p.mac == device_mac), None)

    def cmd(self, what, k):
        self._go((what, k["device_mac"], k["device_model"]))


def plug(mac="AA", nick="Entry Way", online=True, on=False, away=False):
    return NS(mac=mac, nickname=nick, product=NS(model="WLPP1CFH"), is_online=online, is_on=on, away_mode=away)


def wyze_with(sdk, clock=lambda: 0.0):
    logins = []

    def factory(*creds):
        logins.append(creds)
        if isinstance(sdk, Exception):
            raise sdk
        return sdk
    return Wyze("me@example.com", "pw-SECRET", "kid", "key", factory=factory, clock=clock), logins


def test_one_login_for_many_calls_and_rows_are_plain():
    sdk = FakeSdk([plug(on=True, away=True)])
    w, logins = wyze_with(sdk)
    assert w.list() == [{"id": "AA", "model": "WLPP1CFH", "nickname": "Entry Way", "online": True, "on": True, "away": True}]
    w.list()
    w.command("AA", "WLPP1CFH", "off")
    assert len(logins) == 1 and sdk.calls == ["list", "list", ("off", "AA", "WLPP1CFH")]


def test_commands_map_to_the_sdk():
    sdk = FakeSdk([plug()])
    w, _ = wyze_with(sdk)
    for a in ("on", "off", "away_on", "away_off"):
        w.command("AA", "M", a)
    assert [c[0] for c in sdk.calls] == ["on", "off", "away_on", "away_off"]
    with pytest.raises(ValueError):
        w.command("AA", "M", "explode")


def test_auth_error_logs_in_again_once_other_errors_do_not():
    sdk = FakeSdk([plug()])
    w, logins = wyze_with(sdk)
    w.list()
    sdk.fail = RuntimeError("connection reset")
    with pytest.raises(RuntimeError):
        w.list()
    assert len(logins) == 1   # not an auth error: same token
    sdk.fail = RuntimeError("AccessTokenError: token expired")
    with pytest.raises(RuntimeError):
        w.list()
    sdk.fail = None
    w.list()
    assert len(logins) == 2


def test_failed_login_backs_off_without_hammering():
    t = [0.0]
    w, logins = wyze_with(RuntimeError("bad password"), clock=lambda: t[0])
    with pytest.raises(RuntimeError):
        w.list()
    for t[0] in (10.0, 299.0):
        with pytest.raises(Backoff):
            w.list()
    assert len(logins) == 1
    t[0] = 301.0
    with pytest.raises(RuntimeError):
        w.list()
    t[0] = 301.0 + 599
    with pytest.raises(Backoff):   # the wait doubled to 10 min
        w.list()
    assert len(logins) == 2


def test_credentials_never_reach_logs_output_or_repr(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w, _ = wyze_with(RuntimeError("login failed for pw-SECRET me@example.com"))
    svc = PlugService(w, tmp_path / "out", tmp_path / "plugs")
    data = svc.poll()
    assert data == {"plugs": [], "error": "RuntimeError"}
    svc._publish()
    shown = repr(w) + str(data) + caplog.text + (tmp_path / "out" / "wyze.json").read_text()
    assert "SECRET" not in shown and "example.com" not in shown


def test_jobs_only_exist_when_wyze_email_is_set(tmp_path):
    base = {"SPEEDTEST": "0", "SPORTS": "0"}
    assert not [j for j in build_jobs(base.get) if j.name.startswith(("wyze", "plug"))]
    env = {**base, "WYZE_EMAIL": "me@example.com", "COLLECTOR_DIR": str(tmp_path), "PLUGS_DIR": str(tmp_path)}
    jobs = {j.name: j.interval_s for j in build_jobs(env.get)}
    assert {k: jobs[k] for k in ("wyze", "plug_cmd", "plug_schedules")} == {"wyze": 300, "plug_cmd": 2, "plug_schedules": 30}


# ---------- the service: poll, commands, schedules ----------

class FakeWyze:
    def __init__(self, *plugs):
        self.rows = {p["id"]: p for p in plugs}
        self.calls, self.fail_list, self.fail_cmd, self.stale_info = [], None, None, False

    def list(self):
        if self.fail_list:
            raise self.fail_list
        return [dict(r) for r in self.rows.values()]

    def info(self, mac):
        self.calls.append(("info", mac))
        if self.stale_info:
            raise RuntimeError("cloud hiccup")
        return dict(self.rows[mac])

    def command(self, mac, model, action):
        self.calls.append((action, mac))
        if self.fail_cmd:
            raise self.fail_cmd
        key = "away" if action.startswith("away") else "on"
        self.rows[mac][key] = action.endswith("on")

    def cmds(self):
        return [c for c in self.calls if c[0] != "info"]


def row(pid="AA", online=True, on=False, away=False):
    return {"id": pid, "model": "WLPP1CFH", "nickname": "Entry Way", "online": online, "on": on, "away": away}


class Clock:
    def __init__(self, local, tz="America/New_York"):
        self.t = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)

    def __call__(self):
        return self.t

    def add(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def env(tmp_path):
    out, plugs = tmp_path / "out", tmp_path / "plugs"
    out.mkdir()
    (plugs / "cmd").mkdir(parents=True)
    return out, plugs


def service(env, fake, start="2026-10-26T17:29:30", **kw):
    out, plugs = env
    clock = Clock(start)
    svc = PlugService(fake, out, plugs, now=clock, **kw)
    svc.poll()
    return svc, clock


def put_config(env, cfg=GOOD, pid="AA"):
    write_json(env[1] / "config.json", {"plugs": {pid: cfg}})


def test_poll_lists_plugs_and_keeps_last_known_on_failure(env):
    fake = FakeWyze(row(on=True))
    svc, _ = service(env, fake)
    assert svc.poll()["plugs"][0]["on"] is True and svc.poll()["plugs"][0]["last_cmd"] is None
    fake.fail_list = RuntimeError("boom")
    d = svc.poll()
    assert d["error"] == "RuntimeError" and d["plugs"][0]["id"] == "AA"


def test_command_file_runs_then_refreshes_and_publishes(env):
    fake = FakeWyze(row())
    svc, _ = service(env, fake)
    write_command(env[1] / "cmd", "AA", "on", "alex")
    svc.run_commands()
    assert fake.cmds() == [("on", "AA")] and ("info", "AA") in fake.calls
    assert not list((env[1] / "cmd").glob("*"))
    doc = json.loads((env[0] / "wyze.json").read_text())
    p = doc["data"]["plugs"][0]
    assert doc["ok"] and p["on"] is True and p["last_cmd"]["by"] == "alex" and p["last_cmd"]["ok"] is True


def test_command_refresh_failure_still_shows_the_new_state(env):
    fake = FakeWyze(row())
    fake.stale_info = True   # the refresh call fails right after the command
    svc, _ = service(env, fake)
    write_command(env[1] / "cmd", "AA", "away_on", "alex")
    svc.run_commands()
    assert fake.calls[0] == ("away_on", "AA") and svc.data()["plugs"][0]["away"] is True


def test_failed_command_is_recorded_not_raised(env):
    fake = FakeWyze(row())
    fake.fail_cmd = RuntimeError("offline")
    svc, _ = service(env, fake)
    write_command(env[1] / "cmd", "AA", "on", "alex")
    svc.run_commands()
    assert svc.data()["plugs"][0]["last_cmd"]["ok"] is False and ("info", "AA") not in fake.calls


def test_bad_command_files_are_discarded_without_a_call(env):
    fake = FakeWyze(row())
    svc, _ = service(env, fake)
    cmd = env[1] / "cmd"
    write_command(cmd, "ZZ", "on", "alex")                        # unknown id
    write_command(cmd, "AA", "reboot", "alex")                    # unknown action
    (cmd / "1-bad.json").write_text("{nope")                       # bad JSON
    (cmd / "2-list.json").write_text("[1]")                        # wrong shape
    (cmd / "3-big.json").write_text('{"id": "AA", "action": "on", "pad": "' + "x" * 5000 + '"}')
    old = cmd / "4-old.json"
    old.write_text('{"id": "AA", "action": "on", "by": "alex"}')   # stale: older than 2 minutes
    os.utime(old, (time.time() - 300, time.time() - 300))
    svc.run_commands()
    assert fake.cmds() == [] and not list(cmd.glob("*"))


def test_commands_run_in_order(env):
    fake = FakeWyze(row())
    svc, _ = service(env, fake)
    cmd = env[1] / "cmd"
    (cmd / "100-a.json").write_text('{"id": "AA", "action": "on", "by": "b"}')
    (cmd / "200-b.json").write_text('{"id": "AA", "action": "off", "by": "b"}')
    svc.run_commands()
    assert fake.cmds() == [("on", "AA"), ("off", "AA")]


def test_nothing_happens_without_files_or_config(env):
    fake = FakeWyze(row())
    svc, clock = service(env, fake)
    svc.run_commands()
    svc.run_schedules()
    assert fake.calls == []


def test_schedule_fires_once_per_minute(env):
    fake = FakeWyze(row())
    put_config(env)
    svc, clock = service(env, fake)
    svc.run_schedules()   # 17:29:30, not yet
    assert fake.cmds() == []
    clock.add(seconds=30)
    svc.run_schedules()
    clock.add(seconds=30)
    svc.run_schedules()   # same minute again, 17:30:30
    assert fake.cmds() == [("on", "AA")]
    p = svc.data()["plugs"][0]
    assert p["last_sched"]["ok"] is True and p["last_sched"]["action"] == "on" and p["on"] is True
    clock.add(days=1)     # next day: a new minute, fires again, but the plug is already on, so no call
    svc.run_schedules()
    assert fake.cmds() == [("on", "AA")]


def test_schedule_skips_wrong_day_disabled_schedule_and_master_off(env):
    fake = FakeWyze(row())
    svc, clock = service(env, fake, start="2026-10-31T17:30:10")   # a Saturday
    put_config(env)
    svc.run_schedules()
    clock.t = Clock("2026-10-26T17:30:10").t
    put_config(env, {**GOOD, "schedules_enabled": False})
    svc.run_schedules()
    put_config(env, {**GOOD, "schedules": [{**SCHED, "enabled": False}]})
    svc.run_schedules()
    assert fake.calls == []


def test_schedule_skips_the_call_when_state_already_matches(env):
    fake = FakeWyze(row(on=True))
    put_config(env)
    svc, clock = service(env, fake, start="2026-10-26T17:30:10")
    svc.run_schedules()
    assert fake.calls == [] and svc.data()["plugs"][0]["last_sched"]["ok"] is True


def test_offline_plug_is_retried_for_ten_minutes_then_dropped(env):
    fake = FakeWyze(row(online=False))
    put_config(env)
    svc, clock = service(env, fake, start="2026-10-26T17:30:10")
    for _ in range(20):   # 30 s ticks through 17:40
        svc.run_schedules()
        clock.add(seconds=30)
    svc.run_schedules()
    assert fake.calls == []
    assert svc.data()["plugs"][0]["last_sched"] == {"action": "on", "ok": False, "error": "Offline", "at": svc.data()["plugs"][0]["last_sched"]["at"]}
    clock.add(minutes=5)
    svc.run_schedules()   # given up: even if it comes back now, that minute is not run late
    fake.rows["AA"]["online"] = True
    svc.poll()
    svc.run_schedules()
    assert fake.cmds() == []


def test_plug_back_online_within_ten_minutes_gets_the_command(env):
    fake = FakeWyze(row(online=False))
    put_config(env)
    svc, clock = service(env, fake, start="2026-10-26T17:30:10")
    svc.run_schedules()
    clock.add(minutes=4)
    fake.rows["AA"]["online"] = True
    svc.poll()
    svc.run_schedules()
    assert fake.cmds() == [("on", "AA")] and svc.data()["plugs"][0]["last_sched"]["ok"] is True
    svc.run_schedules()
    assert fake.cmds() == [("on", "AA")]


def test_failed_call_is_retried_next_tick_then_recorded(env):
    fake = FakeWyze(row())
    fake.fail_cmd = RuntimeError("cloud down")
    put_config(env)
    svc, clock = service(env, fake, start="2026-10-26T17:30:10")
    svc.run_schedules()
    clock.add(seconds=30)
    svc.run_schedules()
    assert len(fake.cmds()) == 2 and svc.data()["plugs"][0]["last_sched"]["error"] == "RuntimeError"
    fake.fail_cmd = None
    clock.add(seconds=30)
    svc.run_schedules()
    assert len(fake.cmds()) == 3 and svc.data()["plugs"][0]["last_sched"]["ok"] is True


def test_restart_in_the_same_minute_does_not_double_fire(env):
    fake = FakeWyze(row())
    put_config(env)
    svc, clock = service(env, fake, start="2026-10-26T17:30:05")
    svc.run_schedules()
    assert len(fake.cmds()) == 1
    fake.rows["AA"]["on"] = False   # someone turned it off again meanwhile
    clock.add(seconds=20)
    again, _ = service(env, fake, start="2026-10-26T17:30:25")   # a new process, same state files
    again.now = clock
    again.run_schedules()
    assert len(fake.cmds()) == 1


def test_missed_minutes_are_not_replayed_after_a_restart(env):
    fake = FakeWyze(row())
    put_config(env)
    svc, clock = service(env, fake, start="2026-10-26T17:37:00")   # 7 minutes late
    svc.run_schedules()
    assert fake.cmds() == []
    svc2, _ = service(env, fake, start="2026-10-26T17:34:30")      # 4 minutes late: still run
    svc2.run_schedules()
    assert len(fake.cmds()) == 1


def test_spring_forward_skips_the_minute_that_does_not_exist(env):
    fake = FakeWyze(row())
    put_config(env, {**GOOD, "schedules": [{**SCHED, "days": [6], "time": "02:30"}]})
    svc, clock = service(env, fake, start="2026-03-08T01:59:00")   # Sunday; 02:00 jumps to 03:00
    for _ in range(80):
        clock.add(seconds=30)
        svc.run_schedules()
    assert fake.cmds() == []


def test_fall_back_repeated_minute_fires_once(env):
    fake = FakeWyze(row())
    put_config(env, {**GOOD, "schedules": [{**SCHED, "days": [6], "time": "01:30"}]})
    out, plugs = env
    clock = Clock("2026-11-01T05:29:30", tz="UTC")   # 01:29:30 EDT; 01:30 happens again an hour later as EST
    svc = PlugService(fake, out, plugs, now=clock)
    svc.poll()
    for _ in range(130):
        clock.add(seconds=30)
        svc.run_schedules()
        fake.rows["AA"]["on"] = False   # keep the plug off so only the minute check can stop a second run
    assert fake.cmds() == [("on", "AA")]
