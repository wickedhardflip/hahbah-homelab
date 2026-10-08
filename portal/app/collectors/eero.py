"""Eero cloud API (the app's unofficial API). Read-only GETs only; no MACs, Wi-Fi/Thread keys or passwords are kept.

Rate limits: `Eero` caches the network URL and spaces its calls out. `live()` makes 3 calls (run every 5 min);
`daily_usage()` makes about 9 (run hourly). Any HTTP error (401 expired, 429 rate limit) raises and the sidecar records
the job as failed until its next turn: nothing retries.
"""
import hashlib
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api-user.e2ro.com"
GAP_S = 0.5   # pause between calls


def default_get(path: str, token: str, timeout: float = 15) -> dict:
    time.sleep(GAP_S)
    url = path if path.startswith("http") else API + (path if path.startswith("/2.2") else "/2.2" + path)
    req = urllib.request.Request(url, headers={"Cookie": f"s={token}", "User-Agent": "homelab-portal/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["data"]


def _name(d: dict) -> str:
    return d.get("nickname") or d.get("display_name") or d.get("hostname") or "Unknown device"


def _key(url) -> str:
    """A short stable id for a device (hash of Eero's device URL, so the MAC inside it never leaves the collector)."""
    return hashlib.sha1((url or "").encode()).hexdigest()[:10]


def _band(freq) -> str | None:
    return None if not freq else "2.4 GHz" if freq < 3000 else "5 GHz" if freq < 5900 else "6 GHz"


def _series(usage: dict, kind: str) -> list:
    s = next((x for x in usage.get("series", []) if x.get("type") == kind), {})
    return [v["value"] for v in s.get("values", [])]


class Eero:
    def __init__(self, token: str, get=default_get):
        self.token, self.get, self._net = token, get, None

    def _g(self, path: str):
        return self.get(path, self.token)

    @property
    def net(self) -> str:
        if not self._net:
            self._net = self._g("/account")["networks"]["data"][0]["url"]
        return self._net

    def live(self, now=None) -> dict:
        """Nodes + connected devices (3 calls)."""
        now = now or datetime.now(timezone.utc)
        net, eeros, devices = self._g(self.net), self._g(self.net + "/eeros"), self._g(self.net + "/devices")
        on = [d for d in devices if d.get("connected")]
        recent = now - timedelta(hours=24)

        def first_seen(d):
            try:
                return datetime.fromisoformat(d["first_seen"].replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError, AttributeError):
                return None

        rows = []
        for d in on:
            c, u, fs = d.get("connectivity") or {}, d.get("usage") or {}, first_seen(d)
            rows.append({"id": _key(d.get("url")), "name": _name(d), "make": d.get("manufacturer") or d.get("inferred_make"), "type": d.get("device_type"),
                         "ip": d.get("ip"), "wired": d.get("wireless") is False, "node": (d.get("source") or {}).get("location"),
                         "band": _band(c.get("frequency")), "signal_dbm": c.get("signal"), "bars": c.get("score_bars"),
                         "down_mbps": u.get("down_mbps"), "up_mbps": u.get("up_mbps"),
                         "profile": (d.get("profile") or {}).get("name"), "paused": bool(d.get("paused")),
                         "guest": bool(d.get("is_guest")), "new": bool(fs and fs > recent)})
        return {
            "network": net.get("name"), "status": net.get("status"),
            "nodes": [{"name": e.get("location"), "model": e.get("model"), "status": e.get("status"),
                       "gateway": bool(e.get("gateway")), "wired": bool(e.get("wired")), "clients": e.get("connected_clients_count"),
                       "firmware": e.get("os_version"), "update_available": bool(e.get("update_available")),
                       "mesh_bars": e.get("mesh_quality_bars"), "last_reboot": e.get("last_reboot"),
                       "uptime_s": (e.get("uptime") or {}).get("since_last_reboot_s"),
                       "channels": {k: {"channel": v.get("channel"), "width": v.get("channel_width"),
                                        "utilization": v.get("channel_utilization"), "clients": v.get("client_count")}
                                    for k, v in (e.get("radio_channel_stats") or {}).items()}} for e in eeros],
            "devices": {"connected": len(on), "wired": sum(1 for r in rows if r["wired"]), "known": len(devices),
                        "new_24h": sum(1 for r in rows if r["new"]), "paused": sum(1 for r in rows if r["paused"])},
            "clients": rows,
        }

    def daily_usage(self, now=None) -> dict:
        """Last 24 h of data use, speed tests, profiles and network settings (about 9 calls)."""
        now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
        q = f"?start={fmt(now - timedelta(days=1))}&end={fmt(now)}&cadence=hourly&timezone=America/New_York"
        n = self.net
        usage, by_dev, by_node = self._g(n + "/data_usage" + q), self._g(n + "/data_usage/devices" + q), self._g(n + "/data_usage/eeros" + q)
        tests, profiles, updates = self._g(n + "/speedtest"), self._g(n + "/profiles"), self._g(n + "/updates")
        guest, reservations, forwards = self._g(n + "/guestnetwork"), self._g(n + "/reservations"), self._g(n + "/forwards")
        return {
            "usage_24h": {"down_bytes": sum(_series(usage, "download")), "up_bytes": sum(_series(usage, "upload")),
                          "hourly_down": _series(usage, "download"), "hourly_up": _series(usage, "upload"),
                          "since": usage.get("start"), "until": usage.get("end")},
            "devices": {_key(v.get("url")): [v.get("download") or 0, v.get("upload") or 0] for v in by_dev.get("values", [])},
            "node_usage": [{"name": v.get("location"), "down_bytes": v.get("download") or 0, "up_bytes": v.get("upload") or 0}
                           for v in by_node.get("values", [])],
            "speed_tests": [{"down_mbps": t.get("down_mbps"), "up_mbps": t.get("up_mbps"), "at": t.get("date")} for t in tests],
            "profiles": [{"name": p.get("name"), "paused": bool(p.get("paused")), "devices": len(p.get("devices") or []),
                          "default": bool(p.get("default"))} for p in profiles],
            "updates": {"has_update": bool(updates.get("has_update")), "target": updates.get("target_firmware"),
                        "update_hour": updates.get("preferred_update_hour"), "last_started": updates.get("last_update_started")},
            "guest": {"enabled": bool(guest.get("enabled")), "name": guest.get("name")},
            "reservations": [{"ip": r.get("ip"), "name": r.get("description")} for r in reservations],
            "forwards": [{"name": f.get("description"), "port": f.get("gateway_port"), "to_port": f.get("client_port"),
                          "protocol": f.get("protocol"), "ip": f.get("ip"), "enabled": bool(f.get("enabled"))} for f in forwards],
        }

    def history(self, now=None) -> dict:
        """Per-device totals for 7 and 30 days and the network's 30 daily totals (3 calls; run once a day).
        Eero keeps about 30 days per window: wider ranges are refused, and 30 days per device only works hourly."""
        now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
        q = lambda days, cad: (f"?start={fmt(now - timedelta(days=days))}&end={fmt(now)}&cadence={cad}"
                               "&timezone=America/New_York")
        n = self.net
        d7, d30 = self._g(n + "/data_usage/devices" + q(7, "hourly")), self._g(n + "/data_usage/devices" + q(30, "hourly"))
        net30 = self._g(n + "/data_usage" + q(30, "daily"))
        pairs = lambda doc: {_key(v.get("url")): [v.get("download") or 0, v.get("upload") or 0] for v in doc.get("values", [])}
        down, up = _series(net30, "download"), _series(net30, "upload")
        days = [v["time"] for v in next((x for x in net30.get("series", []) if x.get("type") == "download"), {}).get("values", [])]
        return {"d7": pairs(d7), "d30": pairs(d30),
                "daily": [{"day": t, "down": a, "up": b} for t, a, b in zip(days, down, up)]}
