"""Live readings merged onto the snapshot: the certificate, app health, and the collector container's files.

Each apply_* takes what one source produced and updates nodes/edge/alerts in place. Data that is stale (older
than 3x its interval) is still shown, but its stations go "unknown" with the reason in the card.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CERT_WARN, CERT_CRIT = 21, 7
DOMAIN_WARN, DOMAIN_CRIT = 30, 7
BACKUP_CRIT_H = 36
SLOW_FRACTION = 0.5


def fmt_duration(seconds: float) -> str:
    hours = int(seconds) // 3600
    days, h = divmod(hours, 24)
    if not days:
        return f"{h} h"
    return f"{days} day{'s' if days != 1 else ''} {h} h"


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _local(iso: str, tz: str) -> str:
    try:
        d = _parse(iso).astimezone(ZoneInfo(tz))
    except (ValueError, TypeError, AttributeError):
        return str(iso)   # never let one odd timestamp break the whole snapshot
    hour = d.hour % 12 or 12
    return f"{d:%b} {d.day}, {hour}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


def _ago(age_s) -> str:
    if age_s is None:
        return "never"
    if age_s < 120:
        return f"{int(age_s)} s ago"
    if age_s < 7200:
        return f"{int(age_s // 60)} min ago"
    return f"{age_s / 3600:.1f} h ago"


def _alert(alerts, id_, sev, target, kind, message):
    alerts.append({"id": id_, "severity": sev, "target": target, "kind": kind, "message": message, "meta": {}})


def _usable(doc) -> bool:
    return bool(doc) and doc.get("ok") and not doc.get("stale")


def _why(doc) -> str:
    if not doc:
        return "not collected yet"
    if not doc.get("ok"):
        return f"unavailable ({doc.get('error', 'error')}) · last tried {_ago(doc.get('age_s'))}"
    return f"stale · last checked {_ago(doc.get('age_s'))}"


def stations_of(source: str, nodes: dict) -> list:
    """The stations a source owns: a failure of that source turns exactly these grey."""
    if source == "mounts":
        return [n for n in nodes.values() if n["kind"] == "mount"]
    if source == "smart":
        return [n for n in nodes.values() if n["kind"] == "drive"]
    if source == "edge":
        return [n for n in (nodes.get("domain"),) if n]
    if source == "nas":
        return [n for n in (nodes.get("nas"),) if n]
    if source == "cert":
        return [n for n in (nodes.get("caddy"),) if n]
    if source == "health":
        return [n for n in nodes.values() if n["kind"] == "app"]
    return []


def grey(source: str, nodes: dict, why: str) -> None:
    for n in stations_of(source, nodes):
        n["status"] = "unknown"
        n["meta"]["Checked"] = why


SOURCE_DOWN_S = 1800   # a collector file this old means the job (or the collector) has stopped


def apply_sources(files: dict, names, alerts: list) -> None:
    """Silence is an alert too: a source that stopped reporting is Danger, one that reports errors is Caution."""
    for name in names:
        doc = files.get(name)
        if not doc or (doc.get("stale") and (doc.get("age_s") is None or doc["age_s"] > SOURCE_DOWN_S)):
            _alert(alerts, f"live-source-{name}", "crit", name, "source_down",
                   f"The {name} check has stopped reporting, so its stations can't be trusted.")
        elif not doc.get("ok"):
            _alert(alerts, f"live-source-{name}", "warn", name, "source_down",
                   f"The {name} check is failing: {doc.get('error') or 'unknown error'}.")


# ---------- the collector's files ----------

def read_collector(directory: Path, now: datetime) -> dict:
    out = {}
    d = Path(directory)
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            age = (now - _parse(doc["checked_at"])).total_seconds()
            doc.update(age_s=round(age), stale=age > 3 * doc.get("interval_s", 60))
        except Exception:  # noqa: BLE001 (any odd file is "unreadable"; it must never hide the other files)
            doc = {"ok": False, "error": "unreadable", "stale": True, "age_s": None}
        out[f.stem] = doc
    return out


# ---------- certificate + Caddy (portal, every 30 s) ----------

def apply_cert(c: dict, snap: dict, nodes: dict, alerts: list, now: datetime) -> None:
    caddy = nodes.get("caddy")
    if c is None or caddy is None:
        return
    if "error" in c:
        caddy["status"] = "crit"
        caddy["meta"]["Live check"] = f"no answer on 443 ({c['error']})"
        _alert(alerts, "live-caddy", "crit", "caddy", "host_down",
               f"Caddy didn't answer on port 443 ({c['error']}), so every *.hahbah.com app is unreachable.")
        return
    snap["edge"]["cert"] = {**c, "renews_at_days_left": 30}
    caddy["status"] = "good"
    days = math.floor((_parse(c["expires"]) - now).total_seconds() / 86400)
    caddy["meta"]["Certificate"] = f"{', '.join(c['names'])} · {c['issuer']} · {days} days left"
    caddy["meta"]["Live check"] = "answering on 443 (checked every 30 s)"
    if days < CERT_WARN:
        sev = "crit" if days < CERT_CRIT else "warn"
        _alert(alerts, "live-cert", sev, "caddy", "cert_expiring",
               f"The HTTPS certificate expires in {days} days. Caddy normally renews it at 30, so renewal is failing.")


# ---------- app health (portal, every 30 s) ----------

def apply_health(results: dict, snap: dict, nodes: dict, alerts: list) -> None:
    covered = set()
    for app_id, r in results.items():
        if app_id == "ollama":
            continue
        n = nodes.get(app_id)
        if n is None:
            continue
        covered.add(app_id)
        n["meta"]["Health check"] = f"HTTP {r['code']} · {round(r['ms'])} ms" if r["ok"] else f"failing ({r.get('error')})"
        n["status"] = "good" if r["ok"] else "crit"
        if not r["ok"]:
            _alert(alerts, f"live-health-{app_id}", "crit", app_id, "app_down",
                   f"{n['label']} didn't answer its health check ({r.get('error')}).")
    stale_ids = {"a2"} if "social" in covered else set()
    o = results.get("ollama")
    if o is not None and "ollama" in nodes:
        stale_ids.add("a3")
        ol, pc = nodes["ollama"], nodes.get("pc")
        if o["ok"]:
            ol["status"] = "good"
            ol["meta"]["Health check"] = f"HTTP {o['code']} · {round(o['ms'])} ms"
            if o.get("models"):
                ol["meta"]["Models"] = ", ".join(o["models"])
            if pc:
                pc["status"] = "good"
                pc["meta"]["Monitoring"] = "Up (Ollama answering, checked every 30 s)"
        else:
            ol["status"] = "unknown"
            ol["meta"]["Health check"] = f"no answer ({o.get('error')})"
            if pc:
                pc["status"] = "unknown"
                pc["meta"]["Monitoring"] = "Not answering (probably asleep)"
            _alert(alerts, "live-pc", "info", "pc", "unmonitored",
                   f"The Windows PC isn't answering (Ollama: {o.get('error')}). It may just be asleep.")
    alerts[:] = [a for a in alerts if a["id"] not in stale_ids]
    snap["alerts"] = [a for a in snap.get("alerts", []) if a["id"] not in stale_ids]


# ---------- NAS (collector, every 60 s) ----------

def apply_nas(doc: dict, snap: dict, nodes: dict, alerts: list) -> None:
    nas = nodes.get("nas")
    if nas is None or not doc:
        return
    data = doc.get("data")
    if data:
        nas["stats"].update({k: v for k, v in data["stats"].items() if v is not None and v != []})
        nas["meta"]["Uptime"] = fmt_duration(data["uptime_s"])
        main = next((a for a in data["raid"]["arrays"] if a["level"] in ("raid5", "raid6")), None) or \
            (data["raid"]["arrays"] or [None])[0]
        if main:
            level = main["level"].replace("raid", "RAID ")
            nas["meta"]["RAID"] = f"{level} · {main['state'].count('U')} of {len(main['state'])} drives in sync"
    if not _usable(doc):
        nas["status"] = "unknown"
        nas["meta"]["Live stats"] = _why(doc)
        return
    nas["status"] = "good"
    nas["meta"]["Live stats"] = f"every minute · checked {_ago(doc['age_s'])}"
    if not data["raid"]["ok"]:
        bad = ", ".join(f"{a['name']} [{a['state']}]" for a in data["raid"]["arrays"] if not a["ok"])
        _alert(alerts, "live-raid", "crit", "nas", "raid_degraded",
               f"The NAS RAID is degraded ({bad}). A drive dropped out; another failure would lose data.")
    for d in data["stats"]["disks"]:
        if d["pct"] >= 80:
            _alert(alerts, f"live-nas-disk-{d['mount']}", "crit" if d["pct"] >= 90 else "warn", "nas", "disk_high",
                   f"The NAS {d['mount']} volume is {d['pct']}% full.")


# ---------- mounts, drives, speed test, pings (collector; these replaced LegacyMonitor) ----------

def apply_mounts(doc: dict, snap: dict, nodes: dict, alerts: list, tz: str) -> None:
    if not doc or not doc.get("data"):
        grey("mounts", nodes, _why(doc))
        return
    fresh = _usable(doc)
    by_path = {n["meta"].get("Path"): n for n in nodes.values() if n["kind"] == "mount"}
    for path, m in doc["data"].items():
        n = by_path.get(path)
        if n is None:
            continue
        n["meta"]["Mounted"], n["meta"]["Responsive"] = ("Yes" if m["mounted"] else "No"), ("Yes" if m["responsive"] else "No")
        n["meta"]["Checked"] = f"every minute · {_local(m['checked_at'], tz)}" if fresh else _why(doc)
        ok = m["mounted"] and m["responsive"]
        n["status"] = ("good" if ok else "crit") if fresh else "unknown"
        if fresh and not ok:
            _alert(alerts, f"live-mount-{n['id']}", "crit", n["id"], "mount_missing",
                   f"{path} is {'not mounted' if not m['mounted'] else 'mounted but not responding'}: Plex can't read that library.")


def apply_drives(doc: dict, snap: dict, nodes: dict, alerts: list, tz: str) -> None:
    if not doc or not doc.get("data"):
        grey("smart", nodes, _why(doc))
        return
    fresh = _usable(doc)
    for name, d in doc["data"].items():
        n = nodes.get(name)
        if n is None:
            continue
        n["meta"].update({"SMART": d["health"].title(), "Temp": f"{d['temp_c']} °C" if d["temp_c"] is not None else "n/a",
                          "Reallocated sectors": str(d["reallocated"] if d["reallocated"] is not None else "n/a")})
        if d["power_on_hours"] is not None:
            n["meta"]["Power-on"] = f"{d['power_on_hours']:,} h ({d['power_on_hours'] / 8760:.1f} yrs)"
        n["meta"]["Checked"] = f"daily · {_local(d['checked_at'], tz)}" if fresh else _why(doc)
        healthy = d["health"].lower() == "healthy"
        n["status"] = ("good" if healthy else "crit") if fresh else "unknown"
        if fresh and not healthy:
            _alert(alerts, f"live-smart-{name}", "crit", name, "smart_warn",
                   f"Drive {name} reports SMART status '{d['health']}'. Plan to replace it.")


def apply_speed(doc: dict, snap: dict, nodes: dict, alerts: list, tz: str, median: float | None = None) -> None:
    isp = nodes.get("isp")
    if isp is None or not doc or not doc.get("data"):
        if isp is not None:
            isp["meta"]["Last speed test"] = _why(doc)
        return
    s = doc["data"]
    isp["meta"]["Last speed test"] = f"{s['download_mbps']:.1f} down / {s['upload_mbps']:.1f} up Mbps"
    isp["meta"]["Tested"] = _local(s["tested_at"], tz)
    isp["meta"]["Source"] = "speedtest-cli, daily at 4:10 AM"
    if _usable(doc) and median and s["download_mbps"] < SLOW_FRACTION * median:
        _alert(alerts, "live-speed", "warn", "isp", "slow_speed",
               f"The internet speed test came in at {s['download_mbps']:.0f} Mbps, well under the usual {median:.0f}.")


def apply_pings(doc: dict, snap: dict, nodes: dict, alerts: list) -> None:
    if not doc or not doc.get("data"):
        return
    fresh = _usable(doc)
    for kind, node_id in (("router", "router"), ("nas", "nas"), ("central", "central")):
        p, n = doc["data"].get(kind), nodes.get(node_id)
        if not p or n is None:
            continue
        n["meta"]["Ping"] = f"{p['latency_ms']:.2f} ms" if p["reachable"] and p["latency_ms"] is not None else "no reply"
        if fresh and not p["reachable"] and node_id != "central":
            n["status"] = "crit"
            _alert(alerts, f"live-ping-{node_id}", "crit", node_id, "host_down", f"{n['label']} stopped answering ping.")


# ---------- backups (collector, every 5 min) ----------

def apply_backup(doc: dict, snap: dict, nodes: dict, alerts: list, tz: str) -> None:
    """Plex backups (RePlexOn's nightly mirror). Danger: the last night failed, or no good backup for 36 h.
    Caution: the last night took over twice its usual time, or its size moved more than 20 % from the 7-night average."""
    n = nodes.get("replexon")
    if n is None:
        return
    if not doc or not doc.get("data"):
        n["meta"]["Last backup"] = _why(doc)
        return
    b = doc["data"]
    ok = b.get("last_success")
    n["meta"]["Last backup"] = (f"{_local(ok['finished_at'], tz)} · {ok['size_gb']} GB · {ok['duration_min']} min"
                                if ok else "none recorded")
    nights = b.get("nights") or []
    if nights:
        n["meta"]["Last 14 nights"] = f"{sum(1 for x in nights if x['status'] == 'ok')} of {len(nights)} OK"
    r30 = b.get("rate_30d")
    if r30:
        n["meta"]["Last 30 days"] = f"{r30['good']} of {r30['total']} OK"
    if ok and ok.get("changed_gb") is not None:
        n["meta"]["Sent last night"] = f"{ok['changed_gb']} GB" + (f" · {ok['files']:,} files" if ok.get("files") is not None else "")
    if ok and ok.get("db_safe") is not None:
        n["meta"]["Plex DB copy"] = "verified safe" if ok["db_safe"] else "NOT safe (live copy)"
    snaps = b.get("snapshots") or {}
    if snaps.get("count"):
        n["meta"]["Weekly snapshots"] = f"{snaps['count']} kept · newest {snaps['newest']}"
    if b.get("nas_reachable") is not None:
        n["meta"]["Backup target"] = "NAS reachable" if b["nas_reachable"] else "NAS NOT reachable"
    if not _usable(doc):
        n["meta"]["Backup history"] = _why(doc)
        return
    hours, last = b.get("hours_since_success"), b.get("last_run") or {}
    if last.get("status") == "failure":
        _alert(alerts, "live-backup", "crit", "replexon", "backup_stale", "Last night's Plex backup failed. Details are in RePlexOn.")
        return
    if hours is None or hours > BACKUP_CRIT_H:
        what = f"No good Plex backup for {hours:.0f} hours" if hours is not None else "There is no good Plex backup on record"
        _alert(alerts, "live-backup", "crit", "replexon", "backup_stale", f"{what}: last night's run didn't happen.")
        return
    avg = b.get("avg_7d") or {}
    if ok and avg.get("runs", 0) >= 3:
        if avg.get("duration_min") and ok["duration_min"] > 2 * avg["duration_min"]:
            _alert(alerts, "live-backup-slow", "warn", "replexon", "backup_stale",
                   f"Last night's backup took {ok['duration_min']} min, more than twice the usual {avg['duration_min']:.0f}.")
        if avg.get("size_gb") and abs(ok["size_gb"] - avg["size_gb"]) > 0.2 * avg["size_gb"]:
            _alert(alerts, "live-backup-size", "warn", "replexon", "backup_stale",
                   f"Last night's backup was {ok['size_gb']} GB against the usual {avg['size_gb']:.2f} GB: check what changed.")


# ---------- domain + DNS (collector, every 6 h) ----------

def apply_edge(doc: dict, snap: dict, nodes: dict, alerts: list, now: datetime) -> None:
    if not doc or not doc.get("data"):
        grey("edge", nodes, _why(doc))
        if nodes.get("domain"):
            nodes["domain"]["meta"]["Live check"] = _why(doc)
        return
    e, dom = doc["data"], nodes.get("domain")
    snap["edge"]["domain"] = e["domain"]
    dns = snap["edge"]["dns"]
    dns.update(wrong=e["dns"]["wrong"], missing=e["dns"]["missing"], via_eero_ok=e["dns"]["via_eero_ok"])
    if dom is not None:
        d = e["domain"]
        exp = _parse(d["expires"])
        dom["meta"].update({"Renews": f"{exp:%b} {exp.day}, {exp.year} · auto-renew {'on' if d['auto_renew'] else 'OFF'}",
                            "WHOIS privacy": "On" if d["whois_privacy"] else "Off",
                            "Transfer lock": "On" if d["transfer_lock"] else "Off",
                            "Through the Eero": "Resolves correctly" if e["dns"]["via_eero_ok"]
                            else f"Wrong for {', '.join(e['dns'].get('via_eero_bad') or [])}",
                            "Live check": f"every 6 h · checked {_ago(doc['age_s'])}" if _usable(doc) else _why(doc)})
        dom["status"] = "good" if _usable(doc) else "unknown"
    if not _usable(doc):
        return
    days = math.floor((_parse(e["domain"]["expires"]) - now).total_seconds() / 86400)
    if days < DOMAIN_WARN or not e["domain"]["auto_renew"]:
        sev = "crit" if days < DOMAIN_CRIT else "warn"
        _alert(alerts, "live-domain", sev, "domain", "domain_expiring",
               f"hahbah.com expires in {days} days and auto-renew is {'on' if e['domain']['auto_renew'] else 'OFF'}.")
    problems = [f"{n} missing" for n in e["dns"]["missing"]] + [f"{n} points elsewhere" for n in e["dns"]["wrong"]]
    if not e["dns"]["via_eero_ok"]:
        problems.append(f"the Eero answers wrongly for {', '.join(e['dns'].get('via_eero_bad') or [])}")
    if problems:
        _alert(alerts, "live-dns", "warn", "domain", "dns_drift", "DNS needs a look: " + "; ".join(problems) + ".")
