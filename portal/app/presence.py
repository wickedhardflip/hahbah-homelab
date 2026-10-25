"""Presence: who is home, from the Eero device list the collector already writes (eero.json, every 5 min). No network here.

A person is home while any of their devices was seen within `away_after_min` minutes of the latest Eero run (a phone that naps
on Wi-Fi is not "away" at once). Arrival counts at the first fresh eero.json that lists one of their devices. When eero.json is missing, failed or
older than 15 min presence is *unknown*: nobody arrives or leaves until fresh data comes back.

    /plugs/people.json   {"people": [{"id", "name", "devices": ["<hash10>", ...]}], "away_after_min": 10}   (the portal writes it)

Devices are only ever the collector's short hash (`id` in eero.json); MACs never reach this module.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_PEOPLE, MAX_DEVICES, MAX_NAME = 12, 8, 30
AWAY_DEFAULT, AWAY_MAX = 10, 120
EERO_STALE_S = 900   # three missed Eero runs
HISTORY_MAX, SEEN_MAX, SEEN_DAYS = 300, 300, 30
PID_RE, DEV_RE = re.compile(r"[a-z0-9]{1,12}"), re.compile(r"[0-9a-f]{10}")
EMPTY = {"people": [], "devices": {}, "history": [], "eero_at": None, "stale": True, "away_after_min": AWAY_DEFAULT}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError, TypeError):
        return None


def validate_people(doc) -> dict:
    """The clean people file, or ValueError with a plain message."""
    if not isinstance(doc, dict) or set(doc) - {"people", "away_after_min"} or not isinstance(doc.get("people", []), list):
        raise ValueError("Send people and away_after_min.")
    away = doc.get("away_after_min", AWAY_DEFAULT)
    if isinstance(away, bool) or not isinstance(away, int) or not 1 <= away <= AWAY_MAX:
        raise ValueError(f"Away after must be a whole number of minutes from 1 to {AWAY_MAX}.")
    people, ids, owned = [], set(), set()
    if len(doc.get("people", [])) > MAX_PEOPLE:
        raise ValueError(f"At most {MAX_PEOPLE} people.")
    for p in doc.get("people", []):
        if not isinstance(p, dict) or set(p) - {"id", "name", "devices"}:
            raise ValueError("Each person needs an id, a name and devices.")
        pid, name, devs = p.get("id"), p.get("name"), p.get("devices", [])
        if not isinstance(pid, str) or not PID_RE.fullmatch(pid) or pid in ids:
            raise ValueError("Each person needs their own short id (lowercase letters and digits).")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME or not name.isprintable():
            raise ValueError(f"A name is plain text, 1 to {MAX_NAME} characters.")
        if not isinstance(devs, list) or len(devs) > MAX_DEVICES:
            raise ValueError(f"At most {MAX_DEVICES} devices per person.")
        for d in devs:
            if not isinstance(d, str) or not DEV_RE.fullmatch(d):
                raise ValueError("Devices are the short ids shown on the Presence page.")
            if d in owned:
                raise ValueError("A device can belong to one person only.")
            owned.add(d)
        ids.add(pid)
        people.append({"id": pid, "name": name.strip(), "devices": list(devs)})
    return {"people": people, "away_after_min": away}


def read_people(path: Path) -> dict:
    """The people file, or nobody when it is missing or bad (the portal validated it on save; this is the second check)."""
    try:
        return validate_people(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {"people": [], "away_after_min": AWAY_DEFAULT}


def read_eero(out_dir: Path, now: datetime):
    """(connected clients, checked_at) from a fresh, successful eero.json, else (None, None)."""
    try:
        doc = json.loads((Path(out_dir) / "eero.json").read_text(encoding="utf-8"))
        at = parse(doc["checked_at"])
        clients = doc["data"]["clients"] if doc.get("ok") else None
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    if at is None or not isinstance(clients, list) or (now - at).total_seconds() > EERO_STALE_S:
        return None, None
    return [c for c in clients if isinstance(c, dict) and isinstance(c.get("id"), str) and DEV_RE.fullmatch(c["id"])], doc["checked_at"]


def update(state: dict, cfg: dict, clients, eero_at, now: datetime) -> tuple[dict, bool]:
    """One step: (new presence state, changed?). `clients`/`eero_at` come from read_eero (None = unknown)."""
    st = json.loads(json.dumps({**EMPTY, **(state or {})}))   # a copy: the caller decides whether to keep it
    before = json.dumps(st, sort_keys=True)
    devices, fresh = st["devices"], clients is not None
    if fresh and eero_at != st["eero_at"]:
        for c in clients:
            devices[c["id"]] = {"name": str(c.get("name") or "Unknown device")[:60], "band": c.get("band"),
                                "signal_dbm": c.get("signal_dbm"), "wired": bool(c.get("wired")), "node": c.get("node"), "last_seen": eero_at}
        st["eero_at"] = eero_at
    owned = {d for p in cfg["people"] for d in p["devices"]}
    cutoff = now - timedelta(days=SEEN_DAYS)
    keep = sorted(((k, v) for k, v in devices.items() if k in owned or (parse(v.get("last_seen")) or cutoff) > cutoff),
                  key=lambda kv: kv[1].get("last_seen") or "", reverse=True)[:SEEN_MAX]
    st["devices"] = devices = dict(keep)
    st["stale"], st["away_after_min"] = not fresh, cfg["away_after_min"]
    away = timedelta(minutes=cfg["away_after_min"])
    old = {p["id"]: p for p in st["people"]}
    people = []
    for p in cfg["people"]:
        prev = old.get(p["id"]) or {}
        home, since = prev.get("home"), prev.get("since")
        if fresh:
            seen = [parse(devices[d]["last_seen"]) for d in p["devices"] if d in devices]
            ref = parse(st["eero_at"]) or now   # measured on Eero's clock: an Eero run that is merely late never makes anyone leave
            home = any(s is not None and ref - s < away for s in seen)
            if prev.get("home") is None:
                since = iso(now)
            elif home != prev["home"]:
                since = iso(now)
                st["history"].append([since, p["id"], "arrive" if home else "leave"])
        people.append({"id": p["id"], "name": p["name"], "devices": p["devices"], "home": home, "since": since})
    st["people"], st["history"] = people, st["history"][-HISTORY_MAX:]
    return st, json.dumps(st, sort_keys=True) != before


def public(st: dict, admin: bool) -> dict:
    """What GET /api/presence shows. Device rows (names, hashed ids, signal) only for admins; connected = in the latest fresh eero.json."""
    names = {p["id"]: p["name"] for p in st.get("people", [])}
    owner = {d: p["id"] for p in st.get("people", []) for d in p["devices"]}
    out = {"people": st.get("people", []), "stale": bool(st.get("stale", True)), "eero_at": st.get("eero_at"),
           "away_after_min": st.get("away_after_min", AWAY_DEFAULT),
           "history": [{"at": at, "person": pid, "name": names.get(pid, "Someone"), "event": ev}
                       for at, pid, ev in reversed(st.get("history", [])[-100:])]}
    if admin:
        live = None if out["stale"] else out["eero_at"]
        out["devices"] = [{"id": k, **v, "connected": live is not None and v.get("last_seen") == live, "person": owner.get(k)}
                          for k, v in st.get("devices", {}).items()]
    return out
