"""Smart plugs: per-plug settings (labels + hubbah-owned schedules), the command files the portal drops, and the
schedule engine the collector runs. Wyze itself has no schedule or rename API, so those live here.

Files in the shared plugs folder (the portal writes, the collector reads):
    config.json                 {"plugs": {"<id>": {"label", "schedules_enabled", "schedules": [...]}}}
    cmd/<unixms>-<rand>.json    {"id", "action": on|off|away_on|away_off, "by"}   consumed and deleted
The collector keeps plug_sched_state.json in its own out folder so a restart never fires a minute twice.
An offline plug is normal (some live in a drawer): nothing here raises an incident or an alert.
"""
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .collectors.wyze import ACTIONS

log = logging.getLogger("collector")

MAX_SCHEDULES, MAX_LABEL = 20, 40
TIME_RE, SID_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d"), re.compile(r"[A-Za-z0-9]{1,12}")
CMD_MAX_AGE_S = 120   # an older command file is discarded, never run late
REPLAY_MIN, RETRY_MIN = 5, 10   # a minute older than 5 is not replayed after a restart; a failing one is retried for 10, then dropped
STALE_S = 900


def _bool(v, what: str) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"{what} must be true or false.")
    return v


def validate_plug_config(d) -> dict:
    """The clean settings for one plug, or ValueError with a plain message."""
    if not isinstance(d, dict) or set(d) - {"label", "schedules_enabled", "schedules"}:
        raise ValueError("Send label, schedules_enabled and schedules.")
    label = d.get("label", "")
    if not isinstance(label, str) or len(label.strip()) > MAX_LABEL or not label.isprintable():
        raise ValueError(f"The label must be plain text, at most {MAX_LABEL} characters.")
    schedules = d.get("schedules", [])
    if not isinstance(schedules, list) or len(schedules) > MAX_SCHEDULES:
        raise ValueError(f"At most {MAX_SCHEDULES} schedules per plug.")
    clean, seen = [], set()
    for s in schedules:
        if not isinstance(s, dict) or set(s) - {"id", "days", "time", "action", "enabled"}:
            raise ValueError("Each schedule needs id, days, time, action and enabled.")
        sid, days = s.get("id"), s.get("days")
        if not isinstance(sid, str) or not SID_RE.fullmatch(sid) or sid in seen:
            raise ValueError("Each schedule needs its own short id (letters and digits).")
        if (not isinstance(days, list) or not days or len(set(days)) != len(days)
                or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 6 for x in days)):
            raise ValueError("Days are numbers 0 (Monday) to 6 (Sunday), each once.")
        if not isinstance(s.get("time"), str) or not TIME_RE.fullmatch(s["time"]):
            raise ValueError("Time must look like 17:30.")
        if s.get("action") not in ("on", "off"):
            raise ValueError("Action must be on or off.")
        seen.add(sid)
        clean.append({"id": sid, "days": sorted(days), "time": s["time"], "action": s["action"],
                      "enabled": _bool(s.get("enabled", True), "enabled")})
    return {"label": label.strip(), "schedules_enabled": _bool(d.get("schedules_enabled", False), "schedules_enabled"),
            "schedules": clean}


def read_config(path: Path) -> dict:
    """{plug id: clean settings}; a missing file or a bad entry is ignored."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))["plugs"]
        items = list(raw.items())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    out = {}
    for pid, d in items:
        try:
            if isinstance(pid, str) and pid:
                out[pid] = validate_plug_config(d)
        except ValueError:
            pass
    return out


def write_json(path: Path, doc) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)


def write_command(cmd_dir: Path, pid: str, action: str, by: str) -> None:
    write_json(Path(cmd_dir) / f"{int(time.time() * 1000)}-{secrets.token_hex(4)}.json", {"id": pid, "action": action, "by": by})


def next_event(cfg: dict, now: datetime) -> str | None:
    """"Thu 17:30 on": the next enabled schedule of a plug whose master switch is on, in home time."""
    if not cfg.get("schedules_enabled"):
        return None
    base, best = now.replace(tzinfo=None, second=0, microsecond=0), None
    for s in cfg.get("schedules", []):
        if not s["enabled"]:
            continue
        h, m = map(int, s["time"].split(":"))
        for off in range(8):
            day = (base + timedelta(days=off)).date()
            at = datetime(day.year, day.month, day.day, h, m)
            if day.weekday() in s["days"] and at > base:
                if best is None or at < best[0]:
                    best = (at, f"{at:%a} {s['time']} {s['action']}")
                break
    return best[1] if best else None


class PlugService:
    """What the collector runs: `poll` (list the plugs), `run_commands` (portal's files), `run_schedules`.
    `wyze` is a collectors.wyze.Wyze (or a fake with list/info/command); one lock keeps the three jobs from overlapping."""

    def __init__(self, wyze, out_dir: Path, plugs_dir: Path, tz: str = "America/New_York",
                 now=lambda: datetime.now(timezone.utc)):
        self.w, self.out, self.dir, self.tz, self.now = wyze, Path(out_dir), Path(plugs_dir), ZoneInfo(tz), now
        self.rows, self.error, self.lock = {}, None, threading.RLock()
        self.gate = None   # automations.Automations.gate when rules exist: gate(pid, schedule) -> may this due schedule run?
        self.state = {"fired": {}, "tries": {}, "last_cmd": {}, "last_sched": {}}
        try:
            saved = json.loads((self.out / "plug_sched_state.json").read_text(encoding="utf-8"))
            for k in self.state:
                if isinstance(saved.get(k), dict):
                    self.state[k] = saved[k]
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self) -> None:
        try:
            write_json(self.out / "plug_sched_state.json", self.state)
        except OSError as e:
            log.warning("plugs could not save state: %s", type(e).__name__)

    def data(self) -> dict:
        plugs = [{**r, "last_cmd": self.state["last_cmd"].get(pid), "last_sched": self.state["last_sched"].get(pid)}
                 for pid, r in self.rows.items()]
        return {"plugs": plugs, **({"error": self.error} if self.error else {})}

    def _publish(self) -> None:
        """Write wyze.json now (after a command), in the same envelope the sidecar uses."""
        doc = {"checked_at": self.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "interval_s": 300, "ok": True, "data": self.data()}
        try:
            write_json(self.out / "wyze.json", doc)
        except OSError as e:
            log.warning("plugs could not write wyze.json: %s", type(e).__name__)

    def poll(self) -> dict:
        """The `wyze` job (every 5 min). A failed list keeps the last known plugs and says why."""
        with self.lock:
            try:
                self.rows, self.error = {r["id"]: r for r in self.w.list()}, None
            except Exception as e:  # noqa: BLE001 (only the class name is kept, never the message)
                self.error = type(e).__name__
                log.warning("wyze list failed: %s", self.error)
            return self.data()

    def _do(self, pid: str, action: str) -> str | None:
        """One command, then one refresh of that plug. Returns the error class name, or None."""
        row = self.rows[pid]
        try:
            self.w.command(pid, row["model"], action)
        except Exception as e:  # noqa: BLE001
            log.warning("plug command failed: %s", type(e).__name__)
            return type(e).__name__
        key = "away" if action.startswith("away") else "on"
        self.rows[pid] = {**row, key: action.endswith("on")}   # shown at once, replaced by the real state below
        try:
            self.rows[pid] = self.w.info(pid) or self.rows[pid]
        except Exception as e:  # noqa: BLE001
            log.warning("plug refresh failed: %s", type(e).__name__)
        return None

    def run_commands(self) -> None:
        """The `plug_cmd` job (every 2 s): a directory listing, and network only when a command file is there."""
        try:
            files = sorted(p for p in (self.dir / "cmd").glob("*.json") if p.is_file())
        except OSError:
            return
        for f in files:
            try:
                fresh = time.time() - f.stat().st_mtime <= CMD_MAX_AGE_S
                doc = json.loads(f.read_text(encoding="utf-8")[:2000]) if fresh else None
            except (OSError, ValueError):
                doc = None
            f.unlink(missing_ok=True)
            if not isinstance(doc, dict) or doc.get("action") not in ACTIONS or not isinstance(doc.get("id"), str):
                continue
            with self.lock:
                if doc["id"] not in self.rows:
                    continue
                err = self._do(doc["id"], doc["action"])
                by = doc.get("by") if isinstance(doc.get("by"), str) else ""
                self.state["last_cmd"][doc["id"]] = {"action": doc["action"], "by": by[:40], "ok": err is None,
                                                      "at": self.now().strftime("%Y-%m-%dT%H:%M:%SZ")}
                self._save()
                self._publish()

    def run_schedules(self) -> None:
        """The `plug_schedules` job (every 30 s): each due schedule runs once per wall-clock minute."""
        cfg = read_config(self.dir / "config.json")
        if not any(c["schedules_enabled"] for c in cfg.values()):
            return
        now = self.now().astimezone(self.tz).replace(tzinfo=None, second=0, microsecond=0)
        changed = False
        with self.lock:
            for pid, c in cfg.items():
                row = self.rows.get(pid)
                if not c["schedules_enabled"] or row is None:
                    continue
                for s in c["schedules"]:
                    if not s["enabled"] or now.weekday() not in s["days"]:
                        continue
                    h, m = map(int, s["time"].split(":"))
                    age = int((now - now.replace(hour=h, minute=m)).total_seconds() // 60)
                    key, stamp = f"{pid}:{s['id']}", now.replace(hour=h, minute=m).strftime("%Y-%m-%dT%H:%M")
                    if not 0 <= age <= RETRY_MIN or self.state["fired"].get(key) == stamp:
                        continue
                    if self.state["tries"].get(key) != stamp:   # first look at this minute
                        if age > REPLAY_MIN:   # missed (collector was down): not replayed
                            continue
                        self.state["tries"][key] = stamp
                        if self.gate and not self.gate(pid, s):   # an automation's "only when ..." does not hold: skipped, not retried
                            self.state["fired"][key], changed = stamp, True
                            continue
                    changed = True
                    err = None
                    if s["action"] in ("on", "off") and row["on"] == (s["action"] == "on"):
                        pass   # already there: no call
                    elif not row["online"]:
                        err = "Offline"
                    else:
                        err = self._do(pid, s["action"])
                    if err is None or age >= RETRY_MIN:   # done, or out of retries
                        self.state["fired"][key] = stamp
                    self.state["last_sched"][pid] = {"action": s["action"], "ok": err is None, "error": err,
                                                     "at": self.now().strftime("%Y-%m-%dT%H:%M:%SZ")}
                    row = self.rows[pid]
            if changed:
                self._save()
                self._publish()


# ---------- floorplan layout (portal only; stored as a Setting) ----------

MAX_ITEMS, MAX_LINKS = 80, 160
ICONS = ("lamp", "tree", "tv", "fan", "heater", "router", "speaker", "other")
LINK_KINDS = ("power", "wifi", "wired", "other")
ITEM_KINDS = ("plug", "eero", "host", "custom")
CUSTOM_ID = re.compile(r"custom:[a-z0-9]{1,16}")
EMPTY_LAYOUT = {"items": [], "links": []}


def _frac(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise ValueError("Positions must be numbers.")
    return round(min(1.0, max(0.0, float(v))), 4)


def validate_layout(doc, known: set) -> dict:
    """The clean layout, or ValueError. `known` = ids of everything placeable (plus ids already saved)."""
    if not isinstance(doc, dict) or set(doc) - {"items", "links"} or not all(isinstance(doc.get(k, []), list) for k in ("items", "links")):
        raise ValueError("Send items and links.")
    items, links = doc.get("items", []), doc.get("links", [])
    if len(items) > MAX_ITEMS or len(links) > MAX_LINKS:
        raise ValueError(f"At most {MAX_ITEMS} items and {MAX_LINKS} links.")
    out_items, ids = [], set()
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("id"), str) or it.get("kind") not in ITEM_KINDS:
            raise ValueError("Each item needs an id and a kind.")
        iid, kind = it["id"], it["kind"]
        if iid in ids or not iid.startswith(kind + ":"):
            raise ValueError("Item ids must be unique and start with their kind.")
        ids.add(iid)
        row = {"id": iid, "kind": kind, "x": _frac(it.get("x")), "y": _frac(it.get("y"))}
        if kind == "custom":
            label = it.get("label")
            if not CUSTOM_ID.fullmatch(iid) or not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_LABEL \
                    or not label.isprintable() or it.get("icon") not in ICONS:
                raise ValueError(f"A custom item needs a label (at most {MAX_LABEL} characters) and an icon from the list.")
            row.update(label=label.strip(), icon=it["icon"])
        elif iid not in known:
            raise ValueError("That item is not in the live data.")
        out_items.append(row)
    out_links = []
    for ln in links:
        if not isinstance(ln, dict) or ln.get("kind") not in LINK_KINDS or ln.get("a") not in ids or ln.get("b") not in ids \
                or ln["a"] == ln["b"]:
            raise ValueError("Each link joins two different placed items and has a kind: power, wifi, wired or other.")
        out_links.append({"a": ln["a"], "b": ln["b"], "kind": ln["kind"]})
    return {"items": out_items, "links": out_links}
