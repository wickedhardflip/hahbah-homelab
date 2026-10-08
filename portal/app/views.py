"""What each signed-in person may see. Non-admins get Plex counts and totals only, and no admin-only app links."""
import copy
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

THUMB_PATH = re.compile(r"^/library/metadata/\d+/(thumb|art)/\d+$")
PUBLIC_PLEX = ("available", "reason", "totals")


def plex_view(plex: dict, is_admin: bool) -> dict:
    if is_admin or not plex:
        return plex
    out = {k: plex[k] for k in PUBLIC_PLEX if k in plex}
    live = plex.get("live") or {}
    out["live"] = {k: live.get(k, 0) for k in ("stream_count", "transcode_count", "total_bandwidth_kbps")}
    return out


HIDDEN_APP = "jobs"   # what Discreet mode hides (deliberately not described anywhere in the UI)


def discreet(snap: dict, app_id: str = HIDDEN_APP) -> dict:
    """A copy of the snapshot with one app gone from every view: its station, links, alerts, DNS names and labels.
    When the app sits mid-line (two links on one line), those links merge into one that still bends through the
    old spot, so the line keeps its shape with no station on it."""
    out = copy.deepcopy(snap)
    node = next((n for n in out.get("nodes", []) if n["id"] == app_id), None)
    prefixes = (f"{app_id}.", urlsplit((node or {}).get("url") or "").hostname or f"{app_id}.")
    layout = out.setdefault("layout", {})
    via, pos = layout.setdefault("via", {}), layout.setdefault("pos", {})
    at = pos.get(app_id)
    touching = [l for l in out.get("links", []) if app_id in (l["a"], l["b"])]
    lines = layout.get("lines", [])
    for ln in lines:
        mine = [l for l in touching if l["id"] in ln.get("links", [])]
        if len(mine) == 2 and at is not None:
            l1, l2 = sorted(mine, key=lambda l: ln["links"].index(l["id"]))
            a = l1["b"] if l1["a"] == app_id else l1["a"]
            b = l2["b"] if l2["a"] == app_id else l2["a"]
            v1 = via.get(l1["id"], [])
            v1 = v1 if l1["b"] == app_id else list(reversed(v1))
            v2 = via.get(l2["id"], [])
            v2 = v2 if l2["a"] == app_id else list(reversed(v2))
            mid = f"{l1['id']}~{l2['id']}"
            out["links"].append({**l1, "id": mid, "a": a, "b": b})
            via[mid] = v1 + [at] + v2
            i = ln["links"].index(l1["id"])
            ln["links"] = [x for x in ln["links"] if x not in (l1["id"], l2["id"])]
            ln["links"].insert(i, mid)
    gone = {l["id"] for l in touching}
    for ln in lines:
        ln["links"] = [x for x in ln.get("links", []) if x not in gone]
    out["links"] = [l for l in out.get("links", []) if l["id"] not in gone]
    for k in gone:
        via.pop(k, None)
    out["nodes"] = [n for n in out.get("nodes", []) if n["id"] != app_id]
    pos.pop(app_id, None)
    for key in ("labels", "badge_at"):
        if isinstance(layout.get(key), dict):
            layout[key].pop(app_id, None)
    out["alerts"] = [a for a in out.get("alerts", []) if a.get("target") != app_id and a.get("target") not in gone]
    names = lambda text: any(p in text for p in prefixes)
    for a in out["alerts"]:
        if a.get("kind") == "dns_drift" and names(a.get("message", "")):
            head, _, rest = a["message"].partition(": ")
            parts = [x for x in rest.rstrip(".").split("; ") if not names(x)]
            a["message"] = f"{head}: {'; '.join(parts)}." if parts else ""
    out["alerts"] = [a for a in out["alerts"] if a.get("message") and not names(a["message"])]
    for n in out["nodes"]:
        eero = (n.get("meta") or {}).get("Through the Eero", "")
        if eero.startswith("Wrong for ") and names(eero):
            bad = [x for x in eero[len("Wrong for "):].split(", ") if not names(x)]
            n["meta"]["Through the Eero"] = f"Wrong for {', '.join(bad)}" if bad else "Resolves correctly"
    _hide_commit(out)
    dns = (out.get("edge") or {}).get("dns")
    if dns:
        dns["expected"] = [e for e in dns.get("expected", []) if not e.get("name", "").startswith(prefixes)]
        for k in ("wrong", "missing", "via_eero_bad"):
            if k in dns:
                dns[k] = [n for n in dns[k] if not n.startswith(prefixes)]
    return out


def _hide_commit(out: dict) -> None:
    """Commit subjects can name anything; only an admin outside Discreet mode sees them."""
    dep = (out.get("edge") or {}).get("deploy")
    if dep and "message" in dep:
        dep["message"] = ""
    for d in (dep or {}).get("history", []):
        d["message"] = ""
    for a in out.get("alerts", []):
        if a.get("kind") == "deploy_failed":
            a["message"] = "The last deploy failed."


def snapshot_for(snap: dict, is_admin: bool, discreet_mode: bool = False) -> dict:
    if discreet_mode:
        snap = discreet(snap)
    if is_admin:
        return snap
    out = copy.deepcopy(snap)
    _hide_commit(out)
    if (out.get("backup") or {}).get("last_run"):
        out["backup"]["last_run"].pop("error", None)
    if "plex" in out:
        out["plex"] = plex_view(out["plex"], False)
    hidden = set()
    for n in out.get("nodes", []):
        if n.get("admin_only"):
            hidden.add(urlsplit(n.get("url") or "").hostname)
            n.pop("url", None)
    dns = (out.get("edge") or {}).get("dns")
    if dns and hidden:   # admin-only app names stay out of the domain card too
        dns["expected"] = [e for e in dns.get("expected", []) if e.get("name") not in hidden]
        for k in ("wrong", "missing"):
            dns[k] = [name for name in dns.get(k, []) if name not in hidden]
    return out


def apps_for(apps: list, is_admin: bool, discreet_mode: bool = False) -> list:
    apps = [a for a in apps if not (discreet_mode and a.get("id") == HIDDEN_APP)] if discreet_mode else apps
    return apps if is_admin else [a for a in apps if not a.get("admin")]


# ---------- what changed (dashboard footer) + Edge & domain (Settings) ----------
CERT_CAUTION_DAYS, CERT_DANGER_DAYS = 21, 7   # tint only; the real alerts live in merge.py


def _when(iso) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _ago(t: datetime | None, now: datetime) -> str:
    if t is None:
        return ""
    m = max(0, round((now - t).total_seconds() / 60))
    return "just now" if m < 1 else f"{m} min ago" if m < 60 else f"{round(m / 60)} h ago" if m < 1440 else f"{round(m / 1440)} d ago"


def _hides_app(i: dict, app_id: str = HIDDEN_APP) -> bool:
    text = (i.get("message") or "").lower()
    return i.get("target") == app_id or "example app" in text or f"{app_id}." in text


def scrub_incidents(incidents: list, show_subjects: bool, discreet_mode: bool = False) -> list:
    """Incident text for people who may not see everything: incident text can quote a commit subject, and Discreet
    mode drops the hidden app's incidents. Shared by the footer and the /incidents page."""
    if not show_subjects:
        incidents = [{**i, "message": "The last deploy failed." if i.get("kind") == "deploy_failed" else i.get("message", "")}
                     for i in incidents]
    return [i for i in incidents if not _hides_app(i)] if discreet_mode else incidents


def duration(opened, closed) -> str:
    """'' = still open."""
    if closed is None or opened is None:
        return ""
    m = max(0, round((closed - opened).total_seconds() / 60))
    return "under a minute" if m < 1 else f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def whatchanged(snap: dict, incidents: list, now: datetime, tz: str, show_subjects: bool, discreet_mode: bool = False) -> dict:
    """The footer's numbers. `snap` must already have been through snapshot_for (it blanks commit subjects);
    `show_subjects` is false for non-admins and Discreet mode, which also drops the hidden app's incidents."""
    dep = (snap.get("edge") or {}).get("deploy") or {}
    hist = dep.get("history") or ([{k: dep[k] for k in ("commit", "version", "message", "at", "ok")}] if dep.get("commit") else [])
    deploys = [{"commit": d.get("commit", "?"), "version": d.get("version"), "ok": bool(d.get("ok")), "ago": _ago(_when(d.get("at")), now),
                "message": d.get("message", "") if show_subjects else ""} for d in hist[:10]]
    incidents = scrub_incidents(incidents, show_subjects, discreet_mode)
    day = lambda t: t.astimezone(ZoneInfo(tz)).date()
    return {"deployed": deploys[0] if deploys else None, "deploys": deploys,
            "today": sum(1 for i in incidents if i.get("opened_at") and day(i["opened_at"]) == day(now)),
            "incidents": [{"message": i["message"], "severity": i.get("severity"), "open": not i.get("closed_at"),
                           "ago": _ago(i.get("opened_at"), now)} for i in incidents[:10]]}


def edge_panel(edge: dict, now: datetime) -> dict:
    """Certificate and domain expiry for Settings, from the snapshot's `edge` block. None = not read yet."""
    def one(block, **extra):
        t = _when((block or {}).get("expires")) if isinstance(block, dict) else None
        if t is None:
            return None
        days = math.floor((t - now).total_seconds() / 86400)
        return {"days": days, "expires": t.strftime("%b %d, %Y").replace(" 0", " "), **extra,
                "tone": "crit" if days < CERT_DANGER_DAYS else "warn" if days < CERT_CAUTION_DAYS else ""}
    cert, dom = (edge or {}).get("cert"), (edge or {}).get("domain")
    return {"cert": one(cert, issuer=(cert or {}).get("issuer", "unknown")) if cert else None,
            "domain": one(dom, registrar=(dom or {}).get("registrar", "unknown"), auto_renew=(dom or {}).get("auto_renew")) if dom else None}
