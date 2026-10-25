"""Automations: every condition, edges, arming, restart, DST, midnight, the three roadmap examples end to end
(fake eero.json + fake clock + the real plug queue run by PlugService over a fake Wyze). Files only, no network."""
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import automations as auto
from app import presence
from app.automations import Automations, Ctx, check, plug_key, sun_times, validate_rule
from app.plugs import PlugService, write_json
from app.sidecar import build_jobs
from tests.test_plugs import FakeWyze

NY = ZoneInfo("America/New_York")
LOC = {"lat": 42.37, "lon": -71.11}   # a made-up place near Boston
ALEX_PHONE, SAM_PHONE, TV = "a1a1a1a1a1", "b2b2b2b2b2", "c3c3c3c3c3"
HALL, TREE = "AAAA00000001", "AAAA00000002"   # Wyze ids look like MACs: they must never reach the log or the state file
HK, TK = plug_key(HALL), plug_key(TREE)
PEOPLE = {"people": [{"id": "alex", "name": "Alex", "devices": [ALEX_PHONE]}, {"id": "sam", "name": "Sam", "devices": [SAM_PHONE]}],
          "away_after_min": 10}


class Clock:
    def __init__(self, local):
        self.t = datetime.fromisoformat(local).replace(tzinfo=NY).astimezone(timezone.utc)

    def __call__(self):
        return self.t

    def add(self, **kw):
        self.t += timedelta(**kw)

    def local(self):
        return self.t.astimezone(NY).strftime("%Y-%m-%dT%H:%M")


def plug(pid, nick, on=False, online=True):
    return {"id": pid, "model": "WLPP1CFH", "nickname": nick, "online": online, "on": on, "away": False}


class World:
    """The collector's folders, a clock, a fake Wyze run through the real PlugService, and the automations job."""

    def __init__(self, tmp, start="2026-10-26T17:00", plugs=None, people=PEOPLE, home=(ALEX_PHONE, SAM_PHONE)):
        self.out, self.dir = tmp / "out", tmp / "plugs"
        self.out.mkdir(parents=True)
        (self.dir / "cmd").mkdir(parents=True)
        self.clock = Clock(start)
        self.wyze = FakeWyze(*(plugs or [plug(HALL, "Hall lamp"), plug(TREE, "Tree lights")]))
        self.svc = PlugService(self.wyze, self.out, self.dir, now=self.clock)
        self.svc.poll()
        self.svc._publish()
        write_json(self.dir / "people.json", people)
        self.home = list(home)
        self.eero_at = None
        self.eero()
        self.auto = self.boot()

    def boot(self):
        a = Automations(self.out, self.dir, location=lambda: LOC, now=self.clock)
        self.svc.gate = a.gate
        return a

    def eero(self, ids=None):
        if ids is not None:
            self.home = list(ids)
        self.eero_at = self.clock()
        doc = {"checked_at": presence.iso(self.clock()), "interval_s": 300, "ok": True,
               "data": {"clients": [{"id": i, "name": f"Device {i[:2]}", "band": "5 GHz", "signal_dbm": "-50 dBm"} for i in self.home]}}
        (self.out / "eero.json").write_text(json.dumps(doc))

    def rules(self, *rules):
        write_json(self.dir / "rules.json", {"rules": list(rules)})

    def tick(self):
        """One collector pass: automations, then the command queue, the same envelope the sidecar writes."""
        data = self.auto.tick()
        if data is not None:
            doc = {"checked_at": presence.iso(self.clock()), "interval_s": 30, "ok": True, "data": data}
            (self.out / "automations.json").write_text(json.dumps(doc))
        self.svc.run_commands()
        return data

    def run(self, minutes, eero_every=5):
        """Advance minute by minute (two ticks a minute), refreshing eero.json every 5 min like the real collector."""
        for _ in range(minutes * 2):
            self.clock.add(seconds=30)
            if (self.clock() - self.eero_at) >= timedelta(minutes=eero_every):
                self.eero()
            self.tick()

    def log(self):
        return auto.read_log(self.dir / "activity.jsonl", 300)[::-1]   # oldest first

    def on(self, pid=HALL):
        return self.wyze.rows[pid]["on"]


def rule(rid="r1", name="Rule", when=None, then=None, mode="live", enabled=True):
    return {"id": rid, "name": name, "enabled": enabled, "mode": mode, "when": when or [{"type": "house", "who": "nobody", "for_min": 0}],
            "then": then or {"do": "on", "plug": HK}}


# ---------- conditions ----------

def ctx(local="2026-10-26T20:00", people=None, stale=False, plugs=None, loc=LOC):
    now = datetime.fromisoformat(local).replace(tzinfo=NY)
    return Ctx(now, NY, people if people is not None else [
        {"id": "alex", "name": "Alex", "home": True, "since": presence.iso(now - timedelta(minutes=30))},
        {"id": "sam", "name": "Sam", "home": False, "since": presence.iso(now - timedelta(minutes=5))}],
        stale, plugs if plugs is not None else {HK: {"id": HALL, "name": "Hall lamp", "online": True, "on": True},
                                                 TK: {"id": TREE, "name": "Tree lights", "online": False, "on": False}}, loc)


@pytest.mark.parametrize("cond, want", [
    ({"type": "people", "people": ["alex"], "match": "any", "state": "home", "for_min": 0}, True),
    ({"type": "people", "people": ["alex"], "match": "any", "state": "home", "for_min": 31}, False),
    ({"type": "people", "people": ["alex", "sam"], "match": "any", "state": "home", "for_min": 0}, True),
    ({"type": "people", "people": ["alex", "sam"], "match": "all", "state": "home", "for_min": 0}, False),
    ({"type": "people", "people": ["sam"], "match": "any", "state": "away", "for_min": 5}, True),
    ({"type": "people", "people": ["sam"], "match": "any", "state": "away", "for_min": 6}, False),
    ({"type": "people", "people": ["gone"], "match": "any", "state": "away", "for_min": 0}, False),
    ({"type": "house", "who": "anyone", "for_min": 0}, True),
    ({"type": "house", "who": "everyone", "for_min": 0}, False),
    ({"type": "house", "who": "nobody", "for_min": 0}, False),
    ({"type": "time", "from": "19:00", "to": "21:00"}, True),
    ({"type": "time", "from": "20:01", "to": "21:00"}, False),
    ({"type": "time", "from": "22:00", "to": "20:00"}, False),   # wraps; 20:00 is the end, not inside
    ({"type": "time", "from": "19:59", "to": "02:00"}, True),
    ({"type": "sun", "after": "sunset", "offset_min": 0}, True),
    ({"type": "sun", "after": "sunrise", "offset_min": 0}, False),
    ({"type": "days", "days": [0]}, True),   # 2026-10-26 is a Monday
    ({"type": "days", "days": [5, 6]}, False),
    ({"type": "plug", "plug": HK, "state": "on"}, True),
    ({"type": "plug", "plug": HK, "state": "off"}, False),
    ({"type": "plug", "plug": TK, "state": "off"}, False),   # offline: unknown, so false
    ({"type": "plug", "plug": "0000000000", "state": "off"}, False),
])
def test_each_condition(cond, want):
    ok, text = check(auto._cond(cond), ctx())
    assert ok is want and text and "AAAA" not in text


def test_house_conditions_with_everyone_away_and_with_nobody_set_up():
    gone = [{"id": "alex", "name": "Alex", "home": False, "since": "2026-10-26T23:00:00Z"}]
    assert check({"type": "house", "who": "nobody", "for_min": 0}, ctx(people=gone))[0] is True
    assert check({"type": "house", "who": "nobody", "for_min": 0}, ctx(people=[]))[0] is False   # no people: never "nobody home"
    assert check({"type": "house", "who": "anyone", "for_min": 0}, ctx(people=[]))[0] is False


def test_presence_conditions_are_false_while_presence_is_unknown():
    ok, text = check({"type": "house", "who": "anyone", "for_min": 0}, ctx(stale=True))
    assert ok is False and "unknown" in text


def test_sun_conditions_follow_the_day():
    c = ctx("2026-10-26T12:00")
    rise, sset = c.sun(date(2026, 10, 26))
    assert rise.hour in (6, 7) and sset.hour in (17, 18)   # late October near Boston
    for local, after, off, want in [(sset - timedelta(minutes=1), "sunset", 0, False), (sset, "sunset", 0, True),
                                    (sset - timedelta(minutes=30), "sunset", -30, True), (rise - timedelta(minutes=1), "sunset", 0, True),
                                    (rise, "sunrise", 0, True), (rise + timedelta(minutes=10), "sunrise", 15, False)]:
        assert check({"type": "sun", "after": after, "offset_min": off}, ctx(local.isoformat()))[0] is want, (local, after, off)
    assert check({"type": "sun", "after": "sunset", "offset_min": 0}, ctx(loc=None))[0] is False


def test_sun_times_match_the_almanac():
    rise, sset = sun_times(date(2026, 6, 21), 51.5074, -0.1278)   # London midsummer: 03:43 and 20:21 UTC
    assert abs((rise - datetime(2026, 6, 21, 3, 43, tzinfo=timezone.utc)).total_seconds()) < 180
    assert abs((sset - datetime(2026, 6, 21, 20, 21, tzinfo=timezone.utc)).total_seconds()) < 180
    _, west = sun_times(date(2026, 6, 21), 34.05, -118.24)   # Los Angeles sets after midnight UTC: still that evening
    assert west.date() == date(2026, 6, 22) and west.hour == 3
    assert sun_times(date(2026, 6, 21), 78.2, 15.6) == (None, None)   # midnight sun


# ---------- validation ----------

@pytest.mark.parametrize("change, msg", [
    ({"id": "R 1"}, "short id"), ({"name": ""}, "name"), ({"name": "x" * 31}, "name"), ({"mode": "armed"}, "Mode"),
    ({"when": []}, "1 to 6"), ({"when": [{"type": "days", "days": [1]}] * 7}, "1 to 6"),
    ({"when": [{"type": "weather"}]}, "Unknown condition"), ({"when": [{"type": "time", "from": "10:00", "to": "10:00"}]}, "two different"),
    ({"when": [{"type": "house", "who": "anyone", "for_min": 721}]}, "For minutes"),
    ({"when": [{"type": "sun", "after": "sunset", "offset_min": 200}]}, "Sun offset"),
    ({"when": [{"type": "people", "people": [], "state": "home"}]}, "at least one person"),
    ({"when": [{"type": "house", "who": "nobody", "extra": 1}]}, "has only"),
    ({"then": {"do": "toggle", "plug": HK}}, "action"), ({"then": {"do": "on", "plug": HALL}}, "Pick the plug"),
    ({"then": {"do": "on_until", "plug": HK}}, "23:00"), ({"then": {"do": "on", "plug": HK, "until": "23:00"}}, "Only on until"),
    ({"enabled": "yes"}, "Enabled"),
])
def test_bad_rules_are_refused_with_a_plain_message(change, msg):
    with pytest.raises(ValueError, match=msg):
        validate_rule({**rule(), **change})


def test_new_rules_default_to_dry_run_and_unknown_ids_are_refused():
    r = rule()
    del r["mode"]
    assert validate_rule(r)["mode"] == "dry_run"
    with pytest.raises(ValueError, match="not in the live data"):
        validate_rule(rule(), plugs={TK}, people=set())
    with pytest.raises(ValueError, match="Presence page"):
        validate_rule(rule(when=[{"type": "people", "people": ["zed"], "state": "home"}]), plugs={HK}, people={"alex"})


# ---------- edges, arming, conflicts ----------

def test_acts_once_on_the_rising_edge_not_while_true(tmp_path):
    w = World(tmp_path)
    w.rules(rule(name="Away lamp"))
    w.tick()
    w.eero([])   # both leave
    w.run(11)
    assert w.on() and [c[0] for c in w.wyze.cmds()] == ["on"]
    w.wyze.rows[HALL]["on"] = False   # someone switches it off by hand (Wyze app): the rule does not fight back
    w.svc.poll(); w.svc._publish()
    w.run(20)
    assert not w.on() and len(w.wyze.cmds()) == 1
    w.eero([ALEX_PHONE]); w.run(1); w.eero([]); w.run(11)   # back, then away again: a new edge
    assert len(w.wyze.cmds()) == 2 and w.svc.state["last_cmd"][HALL]["by"] == "rule:Away lamp"


def test_a_new_rule_is_armed_without_acting(tmp_path):
    w = World(tmp_path, home=())
    w.run(1)   # nobody home already
    w.rules(rule())
    w.run(5)
    assert w.wyze.cmds() == [] and json.loads((w.out / "automations.json").read_text())["data"]["rules"]["r1"]["was"] is True


def test_editing_a_rule_rearms_it(tmp_path):
    w = World(tmp_path)
    w.rules(rule(when=[{"type": "house", "who": "anyone", "for_min": 0}]))
    w.run(1)
    w.rules(rule(when=[{"type": "house", "who": "everyone", "for_min": 0}]))   # true at once, but just edited: armed only
    w.run(1)
    assert w.wyze.cmds() == []


def test_dry_run_logs_but_sends_nothing(tmp_path):
    w = World(tmp_path)
    w.rules(rule(name="Try me", mode="dry_run"))
    w.tick()
    w.eero([]); w.run(11)
    assert w.wyze.cmds() == [] and not list((w.dir / "cmd").glob("*.json"))
    assert [e["text"] for e in w.log()] == ["Would turn Hall lamp on."] and w.log()[0]["mode"] == "dry_run"


def test_first_rule_wins_a_plug_in_the_same_pass(tmp_path):
    w = World(tmp_path)
    w.rules(rule("r1", "First", then={"do": "on", "plug": HK}), rule("r2", "Second", then={"do": "off", "plug": HK}))
    w.tick()
    w.eero([]); w.run(11)
    texts = [e["text"] for e in w.log()]
    assert texts == ["Turned Hall lamp on.", "Skipped: First already switched Hall lamp just now."] and w.on()


def test_offline_or_missing_plug_is_skipped_and_logged(tmp_path):
    w = World(tmp_path, plugs=[plug(HALL, "Hall lamp", online=False)])
    w.rules(rule("r1", "A"), rule("r2", "B", then={"do": "on", "plug": TK}))
    w.tick()
    w.eero([]); w.run(11)
    assert [(e["text"], e["ok"]) for e in w.log()] == [("Skipped: Hall lamp is offline.", False),
                                                       ("Skipped: the plug is not in the live data.", False)]
    assert w.wyze.cmds() == []


def test_unknown_presence_never_fires_a_departure(tmp_path):
    w = World(tmp_path)
    w.rules(rule())
    w.tick()
    w.run(40, eero_every=10 ** 6)   # the Eero job stops writing: presence goes unknown, nobody "leaves"
    assert w.wyze.cmds() == [] and json.loads((w.out / "automations.json").read_text())["data"]["stale"] is True


# ---------- on until, midnight, restart, DST ----------

def test_on_until_crosses_midnight(tmp_path):
    w = World(tmp_path, start="2026-10-26T20:00")
    w.rules(rule(name="Late lamp", then={"do": "on_until", "plug": HK, "until": "01:00"}))
    w.tick()
    w.eero([]); w.run(11)
    assert w.on() and json.loads((w.out / "automations.json").read_text())["data"]["pending"][0]["at"] == "2026-10-27T01:00"
    while w.clock.local() < "2026-10-27T00:59":
        w.run(1)
    assert w.on()
    w.run(2)
    assert not w.on() and w.log()[-1]["text"] == "Turned Hall lamp off (1:00 AM)."


def test_on_until_does_nothing_when_already_past_the_time(tmp_path):
    w = World(tmp_path, start="2026-10-26T23:20")
    w.rules(rule(then={"do": "on_until", "plug": HK, "until": "23:00"}))
    w.tick()
    w.eero([]); w.run(11)
    assert not w.on() and w.log()[-1]["text"] == "Did nothing: it is already past 11:00 PM."


def test_a_short_restart_keeps_edges_and_a_long_one_rearms(tmp_path):
    w = World(tmp_path)
    w.rules(rule())
    w.run(1)
    w.eero([])   # everyone leaves while the collector restarts
    w.clock.add(minutes=3)
    w.auto = w.boot()
    w.run(10)
    assert len(w.wyze.cmds()) == 1   # the edge after a 3 min restart still counts
    w.wyze.rows[HALL]["on"] = False; w.svc.poll(); w.svc._publish()
    w.eero([ALEX_PHONE]); w.run(1)
    w.eero([]); w.clock.add(minutes=30)   # down for 30 min while the house empties: no late replay
    w.auto = w.boot()
    w.run(15)
    assert len(w.wyze.cmds()) == 1


def test_an_overdue_off_runs_after_a_restart_but_not_hours_late(tmp_path):
    w = World(tmp_path, start="2026-10-26T22:00")
    w.rules(rule(then={"do": "on_until", "plug": HK, "until": "23:00"}))
    w.tick()
    w.eero([]); w.run(11)
    w.clock.add(minutes=60)   # collector down 22:11 to 23:11
    w.auto = w.boot()
    w.eero(); w.tick()
    assert not w.on() and w.log()[-1]["text"].startswith("Turned Hall lamp off")
    w2 = World(tmp_path / "b", start="2026-10-26T22:00")
    w2.rules(rule(then={"do": "on_until", "plug": HK, "until": "23:00"}))
    w2.tick(); w2.eero([]); w2.run(11)
    w2.clock.add(hours=8)
    w2.auto = w2.boot(); w2.eero(); w2.tick()
    assert w2.on() and "Dropped" in w2.log()[-1]["text"]


def test_fall_back_hour_fires_once(tmp_path):
    w = World(tmp_path, start="2026-11-01T00:50")   # clocks go back at 2:00 to 1:00
    w.rules(rule(name="Night", when=[{"type": "time", "from": "01:00", "to": "01:30"}], then={"do": "on", "plug": HK}))
    w.tick()
    seen = []
    for _ in range(150):   # through both 1 o'clock hours
        w.clock.add(seconds=30)
        w.eero()
        w.tick()
        if w.on():
            seen.append(w.clock.local())
            w.wyze.rows[HALL]["on"] = False; w.svc.poll(); w.svc._publish()
    assert seen == ["2026-11-01T01:00"]   # the repeated 1:00 (now EST) does not fire it again


def test_spring_forward_until_time_that_does_not_exist_runs_at_three(tmp_path):
    w = World(tmp_path, start="2026-03-08T01:30")   # 2:00 jumps to 3:00
    w.rules(rule(then={"do": "on_until", "plug": HK, "until": "02:30"}))
    w.tick()
    w.eero([]); w.run(11)
    assert w.on()
    while w.on():
        w.run(1)
    assert w.clock.local() in ("2026-03-08T03:00", "2026-03-08T03:01")


# ---------- the three roadmap examples, end to end ----------

def test_example_1_hall_lamp_follows_its_schedule_only_when_alex_or_sam_is_home(tmp_path):
    w = World(tmp_path, start="2026-10-26T17:00", home=(ALEX_PHONE,))
    write_json(w.dir / "config.json", {"plugs": {HALL: {"label": "Hall lamp", "schedules_enabled": True, "schedules": [
        {"id": "s1", "days": [0, 1, 2, 3, 4, 5, 6], "time": "17:30", "action": "on", "enabled": True}]}}})
    w.rules(rule(name="Home schedule", when=[{"type": "people", "people": ["alex", "sam"], "match": "any", "state": "home", "for_min": 0}],
                 then={"do": "gate", "plug": HK}))
    w.tick()

    def until_1731():
        while w.clock.local() < "2026-10-26T17:31":
            w.clock.add(seconds=30)
            w.eero()
            w.tick()
            w.svc.run_schedules()

    until_1731()
    assert w.on() and w.log()[-1]["text"] == "Let Hall lamp's 5:30 PM on schedule run."
    w2 = World(tmp_path / "b", start="2026-10-26T17:00", home=(TV,))   # nobody home
    write_json(w2.dir / "config.json", json.loads((w.dir / "config.json").read_text()))
    w2.rules(json.loads((w.dir / "rules.json").read_text())["rules"][0])
    w2.tick()
    w = w2
    until_1731()
    assert not w.on() and w.wyze.cmds() == [] and w.log()[-1]["text"].startswith("Skipped Hall lamp's 5:30 PM on schedule. Not true: Alex or Sam")


def test_example_1_in_dry_run_lets_the_schedule_run(tmp_path):
    w = World(tmp_path, start="2026-10-26T17:29", home=())
    write_json(w.dir / "config.json", {"plugs": {HALL: {"label": "Hall lamp", "schedules_enabled": True, "schedules": [
        {"id": "s1", "days": [0], "time": "17:30", "action": "on", "enabled": True}]}}})
    w.rules(rule(mode="dry_run", when=[{"type": "people", "people": ["alex", "sam"], "match": "any", "state": "home", "for_min": 0}],
                 then={"do": "gate", "plug": HK}))
    w.run(11)
    w.clock.t = datetime(2026, 10, 26, 17, 30, 10, tzinfo=NY).astimezone(timezone.utc)
    w.svc.run_schedules()
    assert w.on() and w.log()[-1]["text"].startswith("Would skip Hall lamp's 5:30 PM on schedule")


def test_gate_lets_the_schedule_run_while_presence_is_unknown(tmp_path):
    w = World(tmp_path, start="2026-10-26T17:29", home=())
    write_json(w.dir / "config.json", {"plugs": {HALL: {"label": "", "schedules_enabled": True, "schedules": [
        {"id": "s1", "days": [0], "time": "17:30", "action": "on", "enabled": True}]}}})
    w.rules(rule(when=[{"type": "house", "who": "anyone", "for_min": 0}], then={"do": "gate", "plug": HK}))
    w.tick()
    w.auto.state["presence"]["stale"] = True
    w.clock.t = datetime(2026, 10, 26, 17, 30, 5, tzinfo=NY).astimezone(timezone.utc)
    w.svc.run_schedules()
    assert w.on() and "presence is unknown" in w.log()[-1]["text"]


def test_example_2_everyone_away_keeps_the_hall_lamp_on_until_eleven(tmp_path):
    w = World(tmp_path, start="2026-10-26T18:00")
    w.rules(rule(name="Away lamp", when=[{"type": "house", "who": "nobody", "for_min": 0}],
                 then={"do": "on_until", "plug": HK, "until": "23:00"}))
    w.tick()
    w.eero([SAM_PHONE]); w.run(15)
    assert not w.on()   # one still home
    w.eero([]); w.run(9)
    assert not w.on()   # debounce: not away yet
    w.run(6)
    assert w.on()
    w.eero([ALEX_PHONE]); w.run(30)   # Alex comes back for a bit: the lamp stays on, the 11 PM off stays
    w.eero([]); w.run(60)
    while w.clock.local() < "2026-10-26T22:59":
        w.run(1)
    assert w.on()
    w.run(2)
    assert not w.on() and [c[0] for c in w.wyze.cmds()] == ["on", "off"]
    assert [e["text"] for e in w.log()] == ["Turned Hall lamp on until 11:00 PM.", "Hall lamp was already on until 11:00 PM.",   # left again
                                            "Turned Hall lamp off (11:00 PM)."]


def test_example_3_tree_lights_on_at_sunset_off_at_eleven(tmp_path):
    w = World(tmp_path, start="2026-10-26T16:00")
    w.rules(rule(name="Tree lights", when=[{"type": "sun", "after": "sunset", "offset_min": 0}],
                 then={"do": "on_until", "plug": TK, "until": "23:00"}))
    w.tick()
    _, sset = Ctx(w.clock(), NY, [], False, {}, LOC).sun(date(2026, 10, 26))
    while w.clock.local() < sset.strftime("%Y-%m-%dT%H:%M"):
        assert not w.on(TREE)
        w.run(1)
    w.run(1)
    assert w.on(TREE) and w.log()[0]["text"] == "Turned Tree lights on until 11:00 PM."
    while w.clock.local() < "2026-10-26T23:01":
        w.run(1)
    assert not w.on(TREE)
    w.run(60 * 2)   # past midnight: still night, but no new edge
    assert len(w.wyze.cmds()) == 2


# ---------- log + privacy + wiring ----------

def test_log_rolls_over_and_reads_both_files(tmp_path):
    path = tmp_path / "activity.jsonl"
    for i in range(1500):
        auto.append_log(path, {"at": f"t{i}", "rule": "r1", "name": "x" * 60, "mode": "live", "text": "y" * 60, "ok": True})
    assert (tmp_path / "activity.jsonl.1").exists() and path.stat().st_size <= auto.LOG_MAX + 400
    got = auto.read_log(path, 300)
    assert got[0]["at"] == "t1499" and len(got) == 300
    path.write_text(path.read_text() + "{broken\n")
    assert auto.read_log(path, 1)[0]["at"] == "t1499"


def test_no_wyze_id_or_mac_in_the_log_or_the_state(tmp_path):
    w = World(tmp_path)
    w.rules(rule(name="Away lamp", then={"do": "on_until", "plug": HK, "until": "23:00"}))
    w.tick(); w.eero([]); w.run(15)
    text = (w.dir / "activity.jsonl").read_text() + (w.out / "automations.json").read_text() + (w.dir / "rules.json").read_text()
    assert "AAAA0000" not in text and HALL not in text and TREE not in text


def test_quiet_when_nothing_changes(tmp_path):
    w = World(tmp_path)
    w.rules(rule())
    w.tick()
    assert w.auto.tick() is None   # 0 s later: nothing new
    w.clock.add(minutes=4)
    assert w.auto.tick() is not None   # heartbeat


def test_job_registered_with_the_plugs_folder_and_gate_wired(tmp_path):
    base = {"SPEEDTEST": "0", "SPORTS": "0"}
    assert "automations" not in [j.name for j in build_jobs(base.get)]
    jobs = {j.name: j for j in build_jobs({**base, "PLUGS_DIR": str(tmp_path), "COLLECTOR_DIR": str(tmp_path)}.get)}
    assert jobs["automations"].interval_s == 30 and jobs["automations"].quiet
    env = {**base, "PLUGS_DIR": str(tmp_path), "COLLECTOR_DIR": str(tmp_path), "WYZE_EMAIL": "me@example.com"}
    jobs = {j.name: j for j in build_jobs(env.get)}
    svc = jobs["plug_schedules"].fn.__self__
    assert svc.gate == jobs["automations"].fn.__self__.gate
