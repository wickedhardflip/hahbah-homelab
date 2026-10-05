"""Build the dashboard snapshot: the static topology (layout, lines, last-known values) with live readings on top."""
import copy
import statistics
import logging
import threading
from datetime import datetime, timezone

from . import merge
from .merge import fmt_duration  # noqa: F401  (re-exported; tests and older callers import it from here)

log = logging.getLogger("portal")

DISK_WARN, DISK_CRIT, MEM_WARN = 80, 90, 85


def _safe(source, on_error):
    try:
        return source()
    except Exception as e:  # noqa: BLE001 (deliberately broad: see build())
        return on_error(e)


NOT_SET_UP = {"available": False, "reason": "not set up"}

# The collector container's jobs, in the order the Sources table lists them.
COLLECTOR_SOURCES = (("nas", "Synology NAS over SSH: CPU, memory, temps, RAID, volume, network", "every minute"),
                     ("mounts", "The four NFS media mounts: mounted and answering", "every minute"),
                     ("pings", "Ping: the Eero, the NAS and central", "every minute"),
                     ("smart", "Drive health (SMART) for the NAS's four drives", "daily at 4:05 AM"),
                     ("speed", "Internet speed test", "daily at 4:10 AM"),
                     ("backup", "RePlexOn's backup history", "every 5 min"),
                     ("edge", "Porkbun (domain + DNS records) and DNS answers through the Eero", "every 6 h"))


def _metrics(snap: dict, nodes: dict, files: dict, now) -> dict:
    """The numbers kept once a day for trend lines and the morning digest (None = not known right now)."""
    m: dict = {}
    sp = (files.get("speed") or {}).get("data") or {}
    m["speed_down"], m["speed_up"] = sp.get("download_mbps"), sp.get("upload_mbps")
    nas = (nodes.get("nas") or {}).get("stats") or {}
    disks = nas.get("disks") or []
    m["nas_used_pct"] = disks[0]["pct"] if disks else None
    m["nas_temp_max"] = max(nas["temps_c"]) if nas.get("temps_c") else None
    drives = (files.get("smart") or {}).get("data") or {}
    if drives:
        m["drives_ok"] = sum(1 for d in drives.values() if str(d.get("health", "")).lower() == "healthy")
        m["drives_total"] = len(drives)
    ok = ((files.get("backup") or {}).get("data") or {}).get("last_success") or {}
    m["backup_gb"], m["backup_min"] = ok.get("size_gb"), ok.get("duration_min")
    try:
        exp = datetime.fromisoformat(snap["edge"]["cert"]["expires"].replace("Z", "+00:00"))
        m["cert_days"] = (exp - now).days
    except (KeyError, TypeError, ValueError, AttributeError):
        m["cert_days"] = None
    return m


class SnapshotBuilder:
    def __init__(self, topology: dict, host, eero, deploy, apps, plex=lambda: dict(NOT_SET_UP), cert=lambda: None,
                 health=lambda: {}, collector=lambda: {}, tz: str = "America/New_York", now=lambda: datetime.now(timezone.utc),
                 trends=lambda: {}):
        self.topology, self.host, self.eero, self.deploy, self.apps, self.plex, self.now = topology, host, eero, deploy, apps, plex, now
        self.cert, self.health, self.collector, self.tz, self.trends = cert, health, collector, tz, trends

    def build(self) -> dict:
        snap = copy.deepcopy(self.topology)
        nodes = {n["id"]: n for n in snap["nodes"]}
        links = {l["id"]: l for l in snap["links"]}
        alerts = list(snap.get("alerts", []))
        # Each source is isolated: one sick source must never freeze or break the others.
        host = _safe(self.host, lambda e: {"available": False, "error": f"{type(e).__name__}: {e}"})
        eero = _safe(self.eero, lambda e: {"reachable": False, "error": type(e).__name__})
        deploy = _safe(self.deploy, lambda e: None)
        apps = _safe(self.apps, lambda e: [])
        plex = _safe(self.plex, lambda e: {"available": False, "reason": type(e).__name__})
        cert = _safe(self.cert, lambda e: {"error": type(e).__name__})
        health = _safe(self.health, lambda e: {})
        files = _safe(self.collector, lambda e: {})
        trends = _safe(self.trends, lambda e: {})
        week = [p["v"] for p in (trends.get("speed_down") or [])[-7:]]
        speed_median = statistics.median(week) if week else None
        now = self.now()
        live = []
        if self._host(host, nodes, links, alerts):
            live.append("central")
        if self._eero(eero, nodes, alerts):
            live.append("eero")
        if self._deploy(deploy, snap, alerts):
            live.append("deploy")
        if self._plex(plex, snap, nodes):
            live.append("tautulli")
        steps = (("cert", lambda: merge.apply_cert(cert, snap, nodes, alerts, now)),
                 ("health", lambda: merge.apply_health(health, snap, nodes, alerts)),
                 ("nas", lambda: merge.apply_nas(files.get("nas"), snap, nodes, alerts)),
                 ("mounts", lambda: merge.apply_mounts(files.get("mounts"), snap, nodes, alerts, self.tz)),
                 ("smart", lambda: merge.apply_drives(files.get("smart"), snap, nodes, alerts, self.tz)),
                 ("speed", lambda: merge.apply_speed(files.get("speed"), snap, nodes, alerts, self.tz, speed_median)),
                 ("pings", lambda: merge.apply_pings(files.get("pings"), snap, nodes, alerts)),
                 ("backup", lambda: merge.apply_backup(files.get("backup"), snap, nodes, alerts, self.tz)),
                 ("edge", lambda: merge.apply_edge(files.get("edge"), snap, nodes, alerts, now)))
        for name, step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001 (a malformed document from one source must not take the others down)
                log.warning("merging %s failed: %s", name, type(e).__name__)
                merge.grey(name, nodes, f"unavailable ({type(e).__name__} while reading it)")
        merge.apply_sources(files, [k for k, _, _ in COLLECTOR_SOURCES], alerts)
        if cert and "error" not in cert:
            live.append("cert")
        if health:
            live.append("health")
        live += [k for k, _, _ in COLLECTOR_SOURCES if files.get(k, {}).get("ok") and not files[k].get("stale")]
        for app in apps:
            if app["id"] in nodes:
                nodes[app["id"]]["url"] = app.get("url") or app["lan_url"]
                if app.get("admin"):
                    nodes[app["id"]]["admin_only"] = True
        named = [a for a in apps if a.get("fqdn")]
        if named:
            home = snap["edge"]["dns"]["expected"][0]
            snap["edge"]["dns"]["expected"] = [home] + [{"name": a["fqdn"], "ip": a["edge_ip"], "host": a["host"]} for a in named]
        snap["alerts"] = alerts
        snap["trends"] = trends
        snap["backup"] = (files.get("backup") or {}).get("data")
        sp = files.get("sports") or {}
        snap["sports"] = sp.get("data") if sp.get("ok") and not sp.get("stale") else None   # Red Sox + Celtics cards (every signed-in user)
        wx = files.get("weather") or {}   # not a Source: a failed fetch just hides the chip, it never raises an alert
        snap["weather"] = wx.get("data") if wx.get("ok") and wx.get("age_s") is not None and wx["age_s"] < 3600 else None
        snap["metrics"] = _metrics(snap, nodes, files, now)
        snap["live"] = live
        snap["ts"] = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        live_label = ("Central (/proc, /sys), the Eero (UPnP), the deploy log, Tautulli, every app's health check, "
                      "Caddy's HTTPS certificate")
        snap["sources"] = [{"id": "live", "label": live_label, "cadence": "every 30 s"}] + [
            {"id": k, "label": label, "cadence": f"{every} · " + (
                f"checked {merge._ago(files[k].get('age_s'))}" if files.get(k, {}).get("ok") else
                ("not collected yet" if k not in files else f"failing ({files[k].get('error')})"))}
            for k, label, every in COLLECTOR_SOURCES]
        return snap

    def _host(self, h, nodes, links, alerts) -> bool:
        central = nodes["central"]
        if not h.get("available"):
            central["status"] = "unknown"
            central["meta"]["Live stats"] = f"unavailable ({h.get('error', 'unknown error')})"
            return False
        central["status"] = "good"
        central["stats"].update({k: h[k] for k in ("cpu_pct", "load", "cores", "mem", "swap", "temps_c", "disks", "nics")})
        central["meta"]["Uptime"] = fmt_duration(h["uptime_s"])
        nic, lan = h["nics"][0], links.get("lan_central")
        if lan is not None:
            lan["a_to_b_mbps"], lan["b_to_a_mbps"] = nic["rx_mbps"], nic["tx_mbps"]
            if nic["rx_mbps"] is not None and lan.get("capacity_mbps"):
                lan["util_pct"] = round((nic["rx_mbps"] + nic["tx_mbps"]) / lan["capacity_mbps"] * 100, 4)
        mem_pct = h["mem"]["used_gb"] / h["mem"]["total_gb"] * 100 if h["mem"]["total_gb"] else 0
        if mem_pct >= MEM_WARN:
            alerts.append({"id": "live-mem", "severity": "warn", "target": "central", "kind": "mem_high",
                           "message": f"Central's memory is {mem_pct:.0f}% used.", "meta": {}})
        for d in h["disks"]:
            if d["pct"] >= DISK_WARN:
                sev = "crit" if d["pct"] >= DISK_CRIT else "warn"
                alerts.append({"id": f"live-disk-{d['mount']}", "severity": sev, "target": "central", "kind": "disk_high",
                               "message": f"Central's {d['mount']} disk is {d['pct']}% full.", "meta": {}})
        return True

    def _eero(self, e, nodes, alerts) -> bool:
        router, isp = nodes["router"], nodes["isp"]
        if not e.get("reachable"):
            router["status"], isp["status"] = "crit", "unknown"
            alerts.append({"id": "live-eero", "severity": "crit", "target": "router", "kind": "host_down",
                           "message": f"The Eero didn't answer on the LAN ({e.get('error', 'no reply')}).", "meta": {}})
            return False
        router["status"] = "good"
        router["meta"]["Checked"] = "Live (UPnP)"
        if e.get("connected") is False:
            isp["status"] = "crit"
            alerts.append({"id": "live-wan", "severity": "crit", "target": "isp", "kind": "host_down",
                           "message": f"The Eero reports the internet connection is down ({e.get('last_error') or 'no detail'}).", "meta": {}})
        elif e.get("connected"):
            isp["status"] = "good"
            isp["meta"]["Connection uptime"] = fmt_duration(e.get("uptime_s") or 0)
            if e.get("public_ip"):
                isp["meta"]["Public IP"] = e["public_ip"]
        return True

    def _deploy(self, d, snap, alerts) -> bool:
        if not d:
            return False
        snap["edge"]["deploy"].update({k: d[k] for k in ("commit", "message", "at", "ok")})
        if not d["ok"]:
            alerts.append({"id": "live-deploy", "severity": "warn", "target": "caddy", "kind": "deploy_failed",
                           "message": f"The last deploy failed: {d['message']}", "meta": {}})
        return True

    def _plex(self, p, snap, nodes) -> bool:
        snap["plex"] = p
        t = nodes.get("tautulli")
        if t is None:
            return bool(p.get("available"))
        if p.get("available"):
            t["status"] = "good"
            t["meta"]["Live stats"] = "Live (Tautulli API)"
            return True
        t["status"] = "unknown" if p.get("reason") == "not set up" else "crit"
        t["meta"]["Live stats"] = f"unavailable ({p.get('reason', 'unknown')})"
        return False


class SnapshotStore:
    """Holds the latest snapshot; the collector loop calls refresh() every 30 s."""

    def __init__(self, build):
        self._build, self._latest, self._lock = build, None, threading.Lock()

    def refresh(self) -> dict:
        snap = self._build()
        with self._lock:
            self._latest = snap
        return snap

    def current(self) -> dict:
        with self._lock:
            latest = self._latest
        return latest if latest is not None else self.refresh()
