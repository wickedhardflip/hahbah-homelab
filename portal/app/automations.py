"""If/then rules over the smart plugs, driven by presence, the clock and the sun. Spec: docs/specs/2026-10-24-automations-design.md.

    /plugs/rules.json      {"rules": [{"id", "name", "enabled", "mode": dry_run|live, "when": [conditions], "then": action}]}   (portal writes)
    /plugs/activity.jsonl  one line per thing a rule did or would do (collector appends; 128 KB, then rolled to .1)

The collector job `automations` (every 30 s) updates presence (presence.py) and evaluates the rules. on / off / on_until act on a
rising edge; `gate` is asked by the plug schedule engine when a schedule is due. Live actions go through the plug command queue with
by "rule:<name>"; dry-run rules (the default) only log. Plugs are named by plug_key() here, never by their Wyze id (a MAC).
"""
import hashlib
import json
import logging
import math
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import presence
from .plugs import TIME_RE, read_config, write_command, write_json

log = logging.getLogger("collector")

MAX_RULES, MAX_CONDS, MAX_NAME = 30, 6, 30
LOG_MAX = 128 * 1024
REARM_S, HEARTBEAT_S, LATE_OFF = 300, 240, timedelta(hours=6)
UNTIL_MAX = timedelta(hours=18)
RID_RE, KEY_RE = re.compile(r"[a-z0-9]{1,12}"), re.compile(r"[0-9a-f]{10}")
DAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DO = ("on", "off", "on_until", "gate")


def plug_key(pid: str) -> str:
    """A short stable id for a plug in rules, logs and this API (the Wyze id is a MAC and stays out of them)."""
    return hashlib.sha1(("plug:" + pid).encode()).hexdigest()[:10]


def ampm(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


# ---------- validation ----------

def _int(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{what} must be a whole number from {lo} to {hi}.")
    return v


def _pick(v, options, what):
    if v not in options:
        raise ValueError(f"{what} must be one of: {', '.join(options)}.")
    return v


def _cond(c) -> dict:
    if not isinstance(c, dict):
        raise ValueError("Each condition must be an object.")
    t = c.get("type")
    fields = {"people": {"type", "people", "match", "state", "for_min"}, "house": {"type", "who", "for_min"}, "time": {"type", "from", "to"},
              "sun": {"type", "after", "offset_min"}, "days": {"type", "days"}, "plug": {"type", "plug", "state"}}
    if t not in fields:
        raise ValueError("Unknown condition type.")
    if set(c) - fields[t]:
        raise ValueError(f"A {t} condition has only: {', '.join(sorted(fields[t] - {'type'}))}.")
    if t == "people":
        ppl = c.get("people")
        if not isinstance(ppl, list) or not 1 <= len(ppl) <= presence.MAX_PEOPLE or len(set(ppl)) != len(ppl) \
                or not all(isinstance(p, str) and presence.PID_RE.fullmatch(p) for p in ppl):
            raise ValueError("Pick at least one person.")
        return {"type": t, "people": ppl, "match": _pick(c.get("match", "any"), ("any", "all"), "Match"),
                "state": _pick(c.get("state"), ("home", "away"), "State"), "for_min": _int(c.get("for_min", 0), 0, 720, "For minutes")}
    if t == "house":
        return {"type": t, "who": _pick(c.get("who"), ("anyone", "everyone", "nobody"), "Who"),
                "for_min": _int(c.get("for_min", 0), 0, 720, "For minutes")}
    if t == "time":
        a, b = c.get("from"), c.get("to")
        if not all(isinstance(x, str) and TIME_RE.fullmatch(x) for x in (a, b)) or a == b:
            raise ValueError("A time window needs two different times like 17:30.")
        return {"type": t, "from": a, "to": b}
    if t == "sun":
        return {"type": t, "after": _pick(c.get("after"), ("sunset", "sunrise"), "Sun"),
                "offset_min": _int(c.get("offset_min", 0), -180, 180, "Sun offset")}
    if t == "days":
        days = c.get("days")
        if not isinstance(days, list) or not days or len(set(days)) != len(days) \
                or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 6 for x in days):
            raise ValueError("Days are numbers 0 (Monday) to 6 (Sunday), each once.")
        return {"type": t, "days": sorted(days)}
    if not isinstance(c.get("plug"), str) or not KEY_RE.fullmatch(c["plug"]):
        raise ValueError("Pick a plug.")
    return {"type": t, "plug": c["plug"], "state": _pick(c.get("state"), ("on", "off"), "Plug state")}


def validate_rule(r, plugs: set | None = None, people: set | None = None) -> dict:
    """The clean rule, or ValueError. With `plugs`/`people` (the portal) the ids must also exist."""
    if not isinstance(r, dict) or set(r) - {"id", "name", "enabled", "mode", "when", "then"}:
        raise ValueError("A rule has id, name, enabled, mode, when and then.")
    rid, name = r.get("id"), r.get("name")
    if not isinstance(rid, str) or not RID_RE.fullmatch(rid):
        raise ValueError("Each rule needs a short id (lowercase letters and digits).")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME or not name.isprintable():
        raise ValueError(f"A rule name is plain text, 1 to {MAX_NAME} characters.")
    if not isinstance(r.get("enabled", True), bool):
        raise ValueError("Enabled must be true or false.")
    when = r.get("when")
    if not isinstance(when, list) or not 1 <= len(when) <= MAX_CONDS:
        raise ValueError(f"A rule needs 1 to {MAX_CONDS} conditions.")
    then = r.get("then")
    if not isinstance(then, dict) or then.get("do") not in DO or set(then) - {"do", "plug", "until"}:
        raise ValueError("The action must be on, off, on_until or gate.")
    if not isinstance(then.get("plug"), str) or not KEY_RE.fullmatch(then["plug"]):
        raise ValueError("Pick the plug the rule switches.")
    act = {"do": then["do"], "plug": then["plug"]}
    if then["do"] == "on_until":
        if not isinstance(then.get("until"), str) or not TIME_RE.fullmatch(then["until"]):
            raise ValueError("On until needs a time like 23:00.")
        act["until"] = then["until"]
    elif "until" in then:
        raise ValueError("Only on until takes a time.")
    clean = {"id": rid, "name": name.strip(), "enabled": r.get("enabled", True),
             "mode": _pick(r.get("mode", "dry_run"), ("dry_run", "live"), "Mode"), "when": [_cond(c) for c in when], "then": act}
    if plugs is not None:
        used = {act["plug"]} | {c["plug"] for c in clean["when"] if c["type"] == "plug"}
        if used - plugs:
            raise ValueError("That plug is not in the live data.")
    if people is not None and {p for c in clean["when"] if c["type"] == "people" for p in c["people"]} - people:
        raise ValueError("That person is not on the Presence page.")
    return clean


def validate_rules(doc, plugs: set | None = None, people: set | None = None) -> list:
    if not isinstance(doc, dict) or set(doc) - {"rules"} or not isinstance(doc.get("rules"), list):
        raise ValueError("Send rules.")
    if len(doc["rules"]) > MAX_RULES:
        raise ValueError(f"At most {MAX_RULES} rules.")
    out = [validate_rule(r, plugs, people) for r in doc["rules"]]
    if len({r["id"] for r in out}) != len(out):
        raise ValueError("Rule ids must be unique.")
    return out


def read_rules(path: Path) -> list:
    """Saved rules; a bad one is skipped, a missing or broken file means none."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))["rules"]
        raw = list(raw)[:MAX_RULES]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    out, seen = [], set()
    for r in raw:
        try:
            clean = validate_rule(r)
        except ValueError:
            continue
        if clean["id"] not in seen:
            seen.add(clean["id"])
            out.append(clean)
    return out


# ---------- sun ----------

def sun_times(day: date, lat: float, lon: float):
    """(sunrise, sunset) in UTC for that local date (NOAA / Almanac for Computers formula, about a minute off), None when it doesn't happen."""
    rad, deg = math.radians, math.degrees
    n, lng_h = day.timetuple().tm_yday, lon / 15
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc) - timedelta(hours=lng_h)
    out = []
    for rising in (True, False):
        t = n + ((6 if rising else 18) - lng_h) / 24
        m = 0.9856 * t - 3.289
        lam = (m + 1.916 * math.sin(rad(m)) + 0.020 * math.sin(rad(2 * m)) + 282.634) % 360
        ra = deg(math.atan(0.91764 * math.tan(rad(lam)))) % 360
        ra = (ra + (math.floor(lam / 90) - math.floor(ra / 90)) * 90) / 15
        sin_d = 0.39782 * math.sin(rad(lam))
        cos_h = (math.cos(rad(90.833)) - sin_d * math.sin(rad(lat))) / (math.cos(math.asin(sin_d)) * math.cos(rad(lat)))
        if not -1 <= cos_h <= 1:
            out.append(None)
            continue
        hh = (360 - deg(math.acos(cos_h)) if rising else deg(math.acos(cos_h))) / 15
        ut = (hh + ra - 0.06571 * t - 6.622 - lng_h) % 24
        at = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=ut)
        while at - noon > timedelta(hours=12):   # keep it on the right side of that place's solar noon
            at -= timedelta(days=1)
        while noon - at > timedelta(hours=12):
            at += timedelta(days=1)
        out.append(at)
    return tuple(out)


# ---------- context + conditions ----------

def plug_rows(collector_dir: Path, plugs_dir: Path | None) -> dict:
    """{plug key: {id, name, online, on}} from wyze.json + the labels in config.json. Never raises."""
    try:
        doc = json.loads((Path(collector_dir) / "wyze.json").read_text(encoding="utf-8"))
        rows = [p for p in doc["data"]["plugs"] if isinstance(p, dict) and isinstance(p.get("id"), str)]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    cfg = read_config(Path(plugs_dir) / "config.json") if plugs_dir else {}
    return {plug_key(p["id"]): {"id": p["id"], "name": cfg.get(p["id"], {}).get("label") or p.get("nickname") or "Plug",
                                "online": bool(p.get("online")), "on": bool(p.get("on"))} for p in rows}


class Ctx:
    """What a condition looks at: home wall time (naive), UTC now, people with home/since, plugs by key, sun times."""

    def __init__(self, now: datetime, tz: ZoneInfo, people: list, stale: bool, plugs: dict, loc: dict | None):
        self.utc, self.tz = now.astimezone(timezone.utc), tz
        self.wall = now.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)
        self.people, self.stale, self.plugs, self.loc, self._sun = {p["id"]: p for p in people}, stale, plugs, loc, {}

    def sun(self, d: date):
        """(sunrise, sunset) as naive home wall times for date d, or (None, None) without a location."""
        if d not in self._sun:
            rise, sset = sun_times(d, self.loc["lat"], self.loc["lon"]) if self.loc else (None, None)
            self._sun[d] = tuple(x.astimezone(self.tz).replace(tzinfo=None, second=0, microsecond=0) if x else None for x in (rise, sset))
        return self._sun[d]

    def held(self, pid: str, home: bool, for_min: int) -> bool:
        p = self.people.get(pid)
        if not p or p.get("home") is None or p["home"] != home:
            return False
        since = presence.parse(p.get("since"))
        return for_min == 0 or (since is not None and self.utc - since >= timedelta(minutes=for_min))


def _for(n: int) -> str:
    return f" for {n} min" if n else ""


def check(c: dict, ctx: Ctx) -> tuple[bool, str]:
    """(holds?, a short sentence) for one condition."""
    t = c["type"]
    if t in ("people", "house"):
        if t == "people":
            names = [ctx.people[p]["name"] if p in ctx.people else "someone removed" for p in c["people"]]
            home = c["state"] == "home"
            res = [ctx.held(p, home, c["for_min"]) for p in c["people"]]
            ok = any(res) if c["match"] == "any" else all(res)
            joined = names[0] if len(names) == 1 else (", ".join(names[:-1]) + (" or " if c["match"] == "any" else " and ") + names[-1])
            text = f"{joined} {'is' if len(names) == 1 else 'are' if c['match'] == 'all' else 'is'} {c['state']}{_for(c['for_min'])}"
        else:
            ids = list(ctx.people)
            res = [ctx.held(p, c["who"] != "nobody", c["for_min"]) for p in ids]
            ok = bool(ids) and (any(res) if c["who"] == "anyone" else all(res))
            text = {"anyone": "Anyone is home", "everyone": "Everyone is home", "nobody": "Nobody is home"}[c["who"]] + _for(c["for_min"])
        if ctx.stale:
            return False, text + " (presence unknown right now)"
        return ok, text
    w = ctx.wall
    if t == "time":
        mins = w.hour * 60 + w.minute
        a, b = (int(x[:2]) * 60 + int(x[3:]) for x in (c["from"], c["to"]))
        return (a <= mins < b) if a < b else (mins >= a or mins < b), f"Between {ampm(c['from'])} and {ampm(c['to'])}"
    if t == "sun":
        off = timedelta(minutes=c["offset_min"])
        rise, sset = ctx.sun(w.date())
        word = f"{abs(c['offset_min'])} min {'before' if c['offset_min'] < 0 else 'after'} {c['after']}" if c["offset_min"] else c["after"]
        text = f"After {word}" + (" (until sunrise)" if c["after"] == "sunset" else " (until sunset)")
        if rise is None or sset is None:
            return False, text + " (no location)"
        if c["after"] == "sunset":
            return w >= sset + off or w < rise, text
        return rise + off <= w < sset, text
    if t == "days":
        return w.weekday() in c["days"], "On " + ", ".join(DAY[d] for d in c["days"])
    p = ctx.plugs.get(c["plug"])
    name = p["name"] if p else "an unknown plug"
    return bool(p and p["online"] and p["on"] == (c["state"] == "on")), f"{name} is {c['state']}"


def evaluate(rule: dict, ctx: Ctx) -> tuple[bool, list]:
    res = [check(c, ctx) for c in rule["when"]]
    return all(ok for ok, _ in res), res


def describe(then: dict, plugs: dict) -> str:
    p = plugs.get(then["plug"])
    name = p["name"] if p else "an unknown plug"
    return {"on": f"turn {name} on", "off": f"turn {name} off", "gate": f"let {name} follow its normal schedule",
            "on_until": f"turn {name} on until {ampm(then.get('until', '00:00'))}, then off"}[then["do"]]


def next_until(wall: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    at = wall.replace(hour=h, minute=m)
    return at if at > wall else at + timedelta(days=1)


def has_presence(rule: dict) -> bool:
    return any(c["type"] in ("people", "house") for c in rule["when"])


def dry_run_answer(rule: dict, ctx: Ctx) -> dict:
    """What "test now" shows: each condition, and what the rule would do if it turned true right now."""
    ok, res = evaluate(rule, ctx)
    act = describe(rule["then"], ctx.plugs)
    if rule["then"]["do"] == "gate":
        if ctx.stale and has_presence(rule):
            would, ok = f"Presence is unknown, so the schedule runs as usual: {act}.", True
        else:
            would = f"A schedule due now would run ({act})." if ok else "A schedule due now would be skipped."
        note = "Gate: checked each time one of the plug's own schedules is due. It only matters while that plug uses hubbah schedules."
    else:
        if ok and rule["then"]["do"] == "on_until" and next_until(ctx.wall, rule["then"]["until"]) - ctx.wall > UNTIL_MAX:
            would = f"Nothing: it is already past {ampm(rule['then']['until'])}."
        else:
            would = (act[0].upper() + act[1:] + ".") if ok else "Nothing: not every condition holds."
        note = "The rule acts once when its conditions turn true, not again while they stay true."
    return {"conditions": [{"text": t, "ok": o} for o, t in res], "all": ok, "would": would, "note": note,
            "mode": rule["mode"], "live": rule["mode"] == "live"}


# ---------- the activity log ----------

def append_log(path: Path, entry: dict) -> None:
    path = Path(path)
    try:
        if path.stat().st_size > LOG_MAX:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.warning("automations could not write the activity log: %s", type(e).__name__)


def read_log(path: Path, limit: int = 100) -> list:
    """Newest first, from the log and its rolled .1 file; broken lines are skipped."""
    lines = []
    for p in (Path(str(path) + ".1"), Path(path)):
        try:
            lines += p.read_text(encoding="utf-8").splitlines()
        except OSError:
            pass
    out = []
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
            if len(out) >= limit:
                break
    return out


# ---------- the collector side ----------

class Automations:
    """The `automations` job (tick) and the schedule gate (gate). State lives in /out/automations.json (written by the sidecar from
    tick's return value); `now`, `location` and the folders are injectable for tests."""

    def __init__(self, out_dir: Path, plugs_dir: Path, tz: str = "America/New_York", location=lambda: None,
                 now=lambda: datetime.now(timezone.utc)):
        self.out, self.dir, self.tz, self.location, self.now = Path(out_dir), Path(plugs_dir), ZoneInfo(tz), location, now
        self.lock = threading.RLock()
        self.state = {"presence": dict(presence.EMPTY), "rules": {}, "pending": []}
        self.rearm, self.last_write = True, None
        try:
            doc = json.loads((self.out / "automations.json").read_text(encoding="utf-8"))
            data = doc["data"]
            self.state = {"presence": {k: data[k] for k in presence.EMPTY if k in data},
                          "rules": data.get("rules") if isinstance(data.get("rules"), dict) else {},
                          "pending": [p for p in data.get("pending") or [] if isinstance(p, dict)]}
            saved = presence.parse(data.get("saved_at"))
            self.rearm = saved is None or (now() - saved).total_seconds() > REARM_S   # down too long: no late edges
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def _log(self, rule: dict, mode: str, text: str, ok: bool) -> dict:
        entry = {"at": presence.iso(self.now()), "rule": rule["id"], "name": rule["name"], "mode": mode, "text": text, "ok": ok}
        append_log(self.dir / "activity.jsonl", entry)
        return entry

    def _loc(self):
        try:
            return self.location()
        except Exception:  # noqa: BLE001 (a bad location file only switches the sun conditions off)
            return None

    def _ctx(self, now: datetime) -> Ctx:
        p = self.state["presence"]
        return Ctx(now, self.tz, p.get("people", []), p.get("stale", True), plug_rows(self.out, self.dir), self._loc())

    def _switch(self, rule: dict, mode: str, key: str, action: str, ctx: Ctx, acted: dict, suffix: str = "") -> tuple[str, bool]:
        p = ctx.plugs.get(key)
        if p is None:
            return "Skipped: the plug is not in the live data.", False
        if key in acted:
            return f"Skipped: {acted[key]} already switched {p['name']} just now.", False
        if not p["online"]:
            return f"Skipped: {p['name']} is offline.", False
        acted[key] = rule["name"]
        if p["on"] == (action == "on"):
            return f"{p['name']} was already {action}{suffix}.", True
        if mode == "live":
            try:
                write_command(self.dir / "cmd", p["id"], action, f"rule:{rule['name']}")
            except OSError as e:
                log.warning("automations could not queue a command: %s", type(e).__name__)
                return f"Could not queue the command for {p['name']}.", False
            p["on"] = action == "on"   # later conditions in this pass see the new state
            return f"Turned {p['name']} {action}{suffix}.", True
        return f"Would turn {p['name']} {action}{suffix}.", True

    def _fire(self, rule: dict, ctx: Ctx, acted: dict) -> dict:
        then, mode = rule["then"], rule["mode"]
        if then["do"] in ("on", "off"):
            text, ok = self._switch(rule, mode, then["plug"], then["do"], ctx, acted)
            return self._log(rule, mode, text, ok)
        until = next_until(ctx.wall, then["until"])
        if until - ctx.wall > UNTIL_MAX:
            return self._log(rule, mode, f"Did nothing: it is already past {ampm(then['until'])}.", True)
        text, ok = self._switch(rule, mode, then["plug"], "on", ctx, acted, f" until {ampm(then['until'])}")
        if ok:
            self.state["pending"] = [x for x in self.state["pending"] if x.get("rule") != rule["id"]]
            self.state["pending"].append({"rule": rule["id"], "plug": then["plug"], "at": until.strftime("%Y-%m-%dT%H:%M"), "mode": mode})
        return self._log(rule, mode, text, ok)

    def _pending(self, rules: dict, ctx: Ctx, acted: dict) -> bool:
        changed, keep = False, []
        for x in self.state["pending"]:
            rule = rules.get(x.get("rule"))
            try:
                at = datetime.strptime(x["at"], "%Y-%m-%dT%H:%M")
            except (KeyError, TypeError, ValueError):
                at = None
            if rule is None or not rule["enabled"] or at is None:   # deleted or switched off: its off is cancelled
                changed = True
                continue
            if ctx.wall < at:
                keep.append(x)
                continue
            changed = True
            mode = x.get("mode") if x.get("mode") in ("dry_run", "live") else "dry_run"
            if ctx.wall - at > LATE_OFF:
                self._log(rule, mode, f"Dropped the {ampm(x['at'][11:])} off: the collector was down until long after it.", False)
                continue
            text, ok = self._switch(rule, mode, x["plug"], "off", ctx, acted, f" ({ampm(x['at'][11:])})")
            entry = self._log(rule, mode, text, ok)
            if rule["id"] in self.state["rules"]:
                self.state["rules"][rule["id"]]["last"] = entry
        self.state["pending"] = keep
        return changed

    def tick(self):
        """One pass (every 30 s). Returns the state to save when something changed or the heartbeat is due, else None (quiet)."""
        with self.lock:
            now = self.now()
            cfg = presence.read_people(self.dir / "people.json")
            clients, eero_at = presence.read_eero(self.out, now)
            self.state["presence"], changed = presence.update(self.state["presence"], cfg, clients, eero_at, now)
            rules = read_rules(self.dir / "rules.json")
            byid = {r["id"]: r for r in rules}
            ctx, acted, rstate = self._ctx(now), {}, self.state["rules"]
            changed |= self._pending(byid, ctx, acted)
            stamp = ctx.wall.strftime("%Y-%m-%dT%H:%M")
            for rule in rules:
                st = rstate.get(rule["id"])
                if not rule["enabled"]:
                    if rstate.pop(rule["id"], None) is not None:
                        changed = True
                    continue
                ok, _ = evaluate(rule, ctx)
                sig = hashlib.sha1(json.dumps([rule["when"], rule["then"]], sort_keys=True).encode()).hexdigest()[:12]
                if st is None or st.get("sig") != sig or self.rearm:   # new, edited or after a long restart: arm without acting
                    rstate[rule["id"]] = {"sig": sig, "was": ok, "stamp": (st or {}).get("stamp"), "last": (st or {}).get("last")}
                    changed = True
                    continue
                if rule["then"]["do"] != "gate" and ok and not st.get("was") and st.get("stamp") != stamp:
                    st["last"], st["stamp"] = self._fire(rule, ctx, acted), stamp
                    changed = True
                if ok != st.get("was"):
                    st["was"], changed = ok, True
            for rid in set(rstate) - set(byid):
                del rstate[rid]
                changed = True
            self.rearm = False
            if not changed and self.last_write and (now - self.last_write).total_seconds() < HEARTBEAT_S:
                return None
            self.last_write = now
            return {**self.state["presence"], "rules": rstate, "pending": self.state["pending"], "saved_at": presence.iso(now)}

    def gate(self, pid: str, sched: dict) -> bool:
        """Asked by PlugService.run_schedules when one of plug `pid`'s schedules is due: may it run? Logs what it decided."""
        with self.lock:
            key = plug_key(pid)
            rules = [r for r in read_rules(self.dir / "rules.json") if r["enabled"] and r["then"]["do"] == "gate" and r["then"]["plug"] == key]
            if not rules:
                return True
            ctx = self._ctx(self.now())
            p = ctx.plugs.get(key)
            what = f"{p['name'] if p else 'the plug'}'s {ampm(sched['time'])} {sched['action']} schedule"
            allow = True
            for r in rules:
                ok, res = evaluate(r, ctx)
                if not ok and ctx.stale and has_presence(r):
                    self._log(r, r["mode"], f"Let {what} run: presence is unknown right now.", True)
                    continue
                if ok:
                    self._log(r, r["mode"], f"Let {what} run.", True)
                    continue
                why = next(t for o, t in res if not o)
                if r["mode"] == "live":
                    allow = False
                    self._log(r, "live", f"Skipped {what}. Not true: {why}.", True)
                else:
                    self._log(r, "dry_run", f"Would skip {what}. Not true: {why}.", True)
            return allow
