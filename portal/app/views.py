"""What each signed-in person may see. Non-admins get Plex counts and totals only, and no admin-only app links."""
import copy
import re
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
