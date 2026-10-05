"""Make Porkbun DNS match apps.yaml: an A record for home and every app, pointing at the machine running Caddy.

    python -m app.dnssync ../apps.yaml            # dry run: show what would change
    python -m app.dnssync ../apps.yaml --apply    # make the changes

Only creates or updates A records for names in apps.yaml. Never deletes anything; never touches
the root, www, MX, TXT or NS. Keys come from Windows Credential Manager and are never printed.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .registry import load_site

API = "https://api.porkbun.com/api/json/v3"


def desired(site: dict) -> dict:
    ip = site["hosts"][site["edge_host"]]["ip"]
    names = ["home"] + [a["subdomain"] for a in site["apps"]]
    return {f"{n}.{site['domain']}": ip for n in names}


def plan(want: dict, records: list) -> list:
    actions = []
    for name, ip in sorted(want.items()):
        same = [r for r in records if r["name"] == name]
        a = [r for r in same if r["type"] == "A"]
        cname = [r for r in same if r["type"] == "CNAME"]   # only a CNAME can't sit beside an A record (TXT, MX... can)
        if cname:
            actions.append({"action": "conflict", "name": name, "ip": ip, "current": f"CNAME {cname[0]['content']}"})
        elif not a:
            actions.append({"action": "create", "name": name, "ip": ip, "current": None})
        elif any(r["content"] != ip for r in a):   # editByNameType rewrites every A record of the name to `ip`
            current = ", ".join(sorted(r["content"] for r in a))
            actions.append({"action": "update", "name": name, "ip": ip, "current": current})
        else:
            actions.append({"action": "ok", "name": name, "ip": ip, "current": ip})
    return actions


def _call(path: str, keys: dict, extra: dict | None = None) -> dict:
    req = urllib.request.Request(API + path, json.dumps({**keys, **(extra or {})}).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        body, code = e.read(), e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"status": "ERROR", "message": f"request failed: {type(e).__name__}"}
    try:
        doc = json.loads(body or b"{}")
    except ValueError:   # a proxy or maintenance page instead of JSON
        return {"status": "ERROR", "message": f"HTTP {code}, not JSON"}
    if not isinstance(doc, dict) or (not doc and code >= 400):
        return {"status": "ERROR", "message": f"HTTP {code}"}
    return doc


def main(argv) -> int:
    import keyring
    site = load_site(Path(argv[0]))
    apply = "--apply" in argv
    keys = {"apikey": keyring.get_password("porkbun-apikey", "porkbun"),
            "secretapikey": keyring.get_password("porkbun-secret", "porkbun")}
    if not all(keys.values()):
        print("Porkbun keys not found in Credential Manager.")
        return 2
    domain = site["domain"]
    got = _call(f"/dns/retrieve/{domain}", keys)
    if got.get("status") != "SUCCESS":
        print(f"Couldn't read {domain} records: {got.get('message')}")
        return 1
    actions = plan(desired(site), got["records"])
    for a in actions:
        print(f"{a['action']:9} {a['name']:28} -> {a['ip']}" + (f"   (now: {a['current']})" if a["action"] in ("update", "conflict") else ""))
    if not apply:
        print("Dry run. Add --apply to make these changes.")
        return 0
    for a in actions:
        sub = a["name"][: -len(domain) - 1]
        if a["action"] == "create":
            r = _call(f"/dns/create/{domain}", keys, {"name": sub, "type": "A", "content": a["ip"], "ttl": "600"})
        elif a["action"] == "update":
            r = _call(f"/dns/editByNameType/{domain}/A/{sub}", keys, {"content": a["ip"], "ttl": "600"})
        else:
            continue
        print(f"{a['action']} {a['name']}: {r.get('status')} {r.get('message', '')}".rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
