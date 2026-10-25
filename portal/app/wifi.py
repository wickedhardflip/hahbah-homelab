"""Wi-Fi coverage + interference: a history of what Eero reports, and the numbers the /wifi page draws.

The collector job (`WifiTracker.tick`, every 5 min) reads /collector/eero.json, which the Eero job already writes, and keeps a
7 day history in /collector/wifi.json: signal, band and node per wireless device, and channel / utilization per node radio.
No new cloud calls. Devices are the short hashed ids Eero's job made; MAC addresses never get here.

What Eero's cloud API gives us: signal (dBm), band, the node a device is on, each node's channel, width and utilization.
It does NOT give rates, retries or airtime per device. Neighbor networks come only from an optional one-time scan
uploaded by an admin (`parse_netsh`). Non Wi-Fi noise (microwaves, baby monitors) is not knowable.
"""
import json
import re
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import presence
from .plugs import write_json

KEEP_DAYS = 7
EVENTS_MAX = 200
ROAM_GAP_S = 1800      # a device back after longer than this reconnected; it did not roam
WEAK_DBM, FAIR_DBM = -75, -65
BANDS = ("2.4 GHz", "5 GHz", "6 GHz")
SCAN_MAX = 120


def dbm(v) -> int | None:
    """Eero reports signal as text like "-52 dBm"; accept numbers too. Anything outside a sane range is None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        m = re.match(r"\s*(-?\d+)", v)
        v = int(m.group(1)) if m else None
    if not isinstance(v, (int, float)) or not -120 <= v <= 0:
        return None
    return int(v)


def band_of(key) -> str | None:
    """"2.4 GHz" / "band_5GHz_full" / "2GHz" -> one of BANDS."""
    m = re.search(r"(\d(?:\.\d)?)\s*GHz", str(key or ""), re.I)
    if not m:
        return None
    return {"2": "2.4 GHz", "2.4": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}.get(m.group(1))


def quality(v) -> str:
    return "none" if v is None else "good" if v >= FAIR_DBM else "fair" if v >= WEAK_DBM else "weak"


def _ts(at: datetime) -> int:
    return int(at.timestamp())


def _node_slug(name) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")


EMPTY = {"eero_at": None, "devices": {}, "nodes": {}, "events": []}


class WifiTracker:
    def __init__(self, out_dir: Path, now=lambda: datetime.now(timezone.utc)):
        self.out, self.now, self.lock = Path(out_dir), now, threading.Lock()
        self.state = self._load()

    def _load(self) -> dict:
        try:
            doc = json.loads((self.out / "wifi.json").read_text(encoding="utf-8"))["data"]
            if isinstance(doc, dict) and isinstance(doc.get("devices"), dict):
                return {**EMPTY, **doc}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return json.loads(json.dumps(EMPTY))

    def _read_eero(self, now: datetime):
        try:
            doc = json.loads((self.out / "eero.json").read_text(encoding="utf-8"))
            at, data = presence.parse(doc["checked_at"]), doc["data"]
            if not doc.get("ok") or at is None or not isinstance(data["clients"], list):
                return None, None
        except (OSError, ValueError, KeyError, TypeError):
            return None, None
        if (now - at).total_seconds() > presence.EERO_STALE_S:
            return None, None
        return data, at

    def tick(self):
        """One pass. Returns the state to save when Eero has new data, else None (nothing to do)."""
        with self.lock:
            now = self.now()
            data, at = self._read_eero(now)
            iso = presence.iso(at) if at else None
            if data is None or iso == self.state["eero_at"]:
                return None
            st, t = self.state, _ts(at)
            for c in data["clients"]:
                if not isinstance(c, dict) or c.get("wired") or not presence.DEV_RE.fullmatch(str(c.get("id") or "")):
                    continue
                v, band = dbm(c.get("signal_dbm")), band_of(c.get("band"))
                if v is None:
                    continue
                d = st["devices"].setdefault(c["id"], {"name": "", "node": None, "pts": []})
                last = d["pts"][-1] if d["pts"] else None
                node = c.get("node") if isinstance(c.get("node"), str) else None
                if last and t - last[0] <= ROAM_GAP_S:
                    if d["node"] and node and node != d["node"]:
                        self._event(t, "roam", f'{str(c.get("name"))[:40]} moved from {d["node"]} to {node}')
                    if last[2] is not None and band and BANDS[last[2]] != band:
                        self._event(t, "band", f'{str(c.get("name"))[:40]} switched from {BANDS[last[2]]} to {band}')
                d["name"], d["node"] = str(c.get("name") or "Unknown device")[:60], node
                d["pts"].append([t, v, BANDS.index(band) if band else None])
            for n in data.get("nodes") or []:
                if not isinstance(n, dict) or not n.get("name"):
                    continue
                nd = st["nodes"].setdefault(str(n["name"])[:40], {"radios": {}})
                if nd.get("status") and n.get("status") and nd["status"] != n["status"]:
                    self._event(t, "node", f'{n["name"]} is now {n["status"]}')
                nd["status"], nd["mesh_bars"], nd["model"] = n.get("status"), n.get("mesh_bars"), n.get("model")
                for key, r in (n.get("channels") or {}).items():
                    band = band_of(key)
                    if not band or not isinstance(r, dict):
                        continue
                    rad = nd["radios"].setdefault(band, {"channel": None, "width": None, "pts": []})
                    if rad["channel"] is not None and r.get("channel") not in (None, rad["channel"]):
                        self._event(t, "channel", f'{n["name"]} {band} moved from channel {rad["channel"]} to {r["channel"]}')
                    if r.get("channel") is not None:
                        rad["channel"], rad["width"] = r["channel"], r.get("width")
                    u = r.get("utilization")
                    rad["pts"].append([t, u if isinstance(u, (int, float)) and not isinstance(u, bool) else None, r.get("clients")])
            cut = t - KEEP_DAYS * 86400
            for k in list(st["devices"]):
                d = st["devices"][k]
                d["pts"] = [p for p in d["pts"] if p[0] >= cut]
                if not d["pts"]:
                    del st["devices"][k]
            for nd in st["nodes"].values():
                for rad in nd["radios"].values():
                    rad["pts"] = [p for p in rad["pts"] if p[0] >= cut]
            st["events"], st["eero_at"] = st["events"][-EVENTS_MAX:], iso
            return json.loads(json.dumps(st))

    def _event(self, t: int, kind: str, text: str) -> None:
        self.state["events"].append([t, kind, text])


# ---------- what the page shows ----------

def _hourly(pts, now_t: int, hours: int) -> list:
    """[[hour start, average dBm]] for the last `hours` hours (gaps simply missing)."""
    buckets: dict = {}
    for t, v, *_ in pts:
        if t >= now_t - hours * 3600:
            buckets.setdefault(t // 3600 * 3600, []).append(v)
    return [[h, round(sum(vs) / len(vs))] for h, vs in sorted(buckets.items())]


def _hour_of_day(pts, tz) -> list:
    """24 averages (None where never seen at that hour) of the history by local hour: the 'typical day' the slider replays."""
    by = [[] for _ in range(24)]
    for t, v, *_ in pts:
        by[datetime.fromtimestamp(t, tz).hour].append(v)
    return [round(sum(x) / len(x)) if x else None for x in by]


def summarize(state: dict, now: datetime, tz, admin: bool) -> dict:
    """Plain data for GET /api/wifi. Device rows only for admins (names are private). Never raises on a partial state."""
    now_t = _ts(now)
    eero_at = presence.parse(state.get("eero_at"))
    stale = eero_at is None or (now - eero_at).total_seconds() > presence.EERO_STALE_S
    nodes, util_by_hour = [], [[] for _ in range(24)]
    for name, nd in sorted((state.get("nodes") or {}).items()):
        radios = {}
        for band, r in nd.get("radios", {}).items():
            pts = r.get("pts") or []
            latest = pts[-1] if pts else None
            radios[band] = {"channel": r.get("channel"), "width": r.get("width"), "utilization": latest[1] if latest else None,
                            "clients": latest[2] if latest else None}
            if band == "2.4 GHz":
                for t, u, *_ in pts:
                    if u is not None:
                        util_by_hour[datetime.fromtimestamp(t, tz).hour].append(u)
        nodes.append({"name": name, "slug": _node_slug(name), "model": nd.get("model"), "status": nd.get("status"),
                      "mesh_bars": nd.get("mesh_bars"), "radios": radios})
    out = {"eero_at": state.get("eero_at"), "stale": stale, "nodes": nodes,
           "busy_by_hour": [round(sum(u) / len(u)) if u else None for u in util_by_hour],
           "events": [{"at": t, "kind": k, "text": x} for t, k, x in reversed((state.get("events") or [])[-40:])]}
    if not admin:
        out["events"] = [e for e in out["events"] if e["kind"] in ("channel", "node")]
        return out
    devices = []
    for k, d in (state.get("devices") or {}).items():
        pts = d.get("pts") or []
        if not pts:
            continue
        recent = [p[1] for p in pts if p[0] >= now_t - 3600]
        med = round(statistics.median(recent)) if recent else None
        last_day = [p[1] for p in pts if p[0] >= now_t - 86400]
        band = BANDS[pts[-1][2]] if pts[-1][2] is not None else None
        connected = not stale and pts[-1][0] >= now_t - 900
        devices.append({"id": k, "name": d.get("name") or "Unknown device", "node": d.get("node"), "band": band, "connected": connected,
                        "dbm": pts[-1][1] if connected else None, "median": med, "quality": quality(med if connected else None),
                        "low_24h": min(last_day) if last_day else None, "high_24h": max(last_day) if last_day else None,
                        "hourly": _hourly(pts, now_t, KEEP_DAYS * 24), "hod": _hour_of_day(pts, tz)})
    devices.sort(key=lambda x: (x["median"] is None, x["median"] if x["median"] is not None else 0))
    out["devices"] = devices
    out["weak"] = [{"id": d["id"], "name": d["name"], "node": d["node"], "dbm": d["median"]} for d in devices
                   if d["connected"] and d["quality"] == "weak"]
    return out


# ---------- neighbor scan (optional, uploaded by an admin) ----------

def channel_band(ch: int) -> str:
    return "2.4 GHz" if ch <= 14 else "5 GHz"   # netsh does not say 6 GHz apart; those rarely show up in a neighbor scan


def parse_netsh(text: str) -> list:
    """Read `netsh wlan show networks mode=bssid`. Keeps ssid, channel, band and signal only: BSSIDs (MACs) are dropped."""
    nets, ssid = [], ""
    cur = None
    for line in str(text).splitlines():
        m = re.match(r"\s*SSID\s+\d+\s*:\s*(.*)$", line)
        if m:
            ssid, cur = m.group(1).strip(), None
            continue
        if re.match(r"\s*BSSID\s+\d+\s*:", line):
            cur = {"ssid": ssid[:32] or "(hidden)", "signal_pct": None, "channel": None}
            nets.append(cur)
            continue
        m = re.match(r"\s*(Signal|Channel)\s*:\s*(\d+)", line)
        if m and cur is not None:
            cur["signal_pct" if m.group(1) == "Signal" else "channel"] = int(m.group(2))
    return clean_scan([{"ssid": n["ssid"], "channel": n["channel"], "dbm": None if n["signal_pct"] is None else round(n["signal_pct"] / 2 - 100)}
                       for n in nets])


def clean_scan(nets) -> list:
    """Validate a list of {ssid, channel, dbm}; raises ValueError when nothing usable is in it."""
    if not isinstance(nets, list):
        raise ValueError("Send a list of networks or the netsh text.")
    out = []
    for n in nets:
        if not isinstance(n, dict) or isinstance(n.get("channel"), bool) or not isinstance(n.get("channel"), int) or not 1 <= n["channel"] <= 233:
            continue
        v = dbm(n.get("dbm"))
        out.append({"ssid": str(n.get("ssid") or "(hidden)")[:32], "channel": n["channel"], "band": channel_band(n["channel"]), "dbm": v})
    if not out:
        raise ValueError("No networks with a channel were found in that.")
    return out[:SCAN_MAX]


def interference(nodes: list, scan: list | None) -> dict:
    """Neighbor crowding per our radios. 2.4 GHz channels overlap (1, 6, 11 are the clean ones); 5 GHz counts same channel only."""
    if not scan:
        return {"scanned": False}
    c24 = {i: 0 for i in range(1, 14)}
    for n in scan:
        if n["band"] == "2.4 GHz" and n["channel"] in c24:
            c24[n["channel"]] += 1

    def load(ch):   # neighbors on or overlapping a channel, nearer ones count more
        return sum(w * (1 - abs(c - ch) / 5) for c, w in c24.items() if abs(c - ch) < 5)

    ours = sorted({r["channel"] for nd in nodes for b, r in nd["radios"].items() if b == "2.4 GHz" and r.get("channel")})
    best = min((1, 6, 11), key=load)
    res = {"scanned": True, "channels_24": c24, "ours_24": ours, "ours_load": {str(c): round(load(c), 1) for c in ours},
           "best_24": best, "suggestion": None,
           "same_5": {str(r["channel"]): sum(1 for n in scan if n["band"] == "5 GHz" and n["channel"] == r["channel"])
                      for nd in nodes for b, r in nd["radios"].items() if b == "5 GHz" and r.get("channel")}}
    if ours and load(best) < min(load(c) for c in ours) * 0.6 and best not in ours:
        res["suggestion"] = (f"Channel {best} has about {load(best):.1f} overlapping neighbors against {min(load(c) for c in ours):.1f} on ours. "
                             "Eero picks channels on its own and this app cannot change it; restarting the gateway node usually makes it choose again.")
    return res
