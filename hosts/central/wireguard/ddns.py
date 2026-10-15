"""Keep <DDNS_NAME>.example.com (A record at Porkbun) equal to the home public IP.

Runs on the host as root from ddns.timer. WAN IP comes from the Eero local UPnP (no external lookup).
Keys come from env PORKBUN_API_KEY / PORKBUN_API_SECRET_KEY and are never printed, logged or written.
At most 2 Porkbun calls per run; no retries (the next timer tick is the retry).
"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "portal"))
from app.collectors import upnp  # noqa: E402  (stdlib-only module)

API = "https://api.porkbun.com/api/json/v3"
DOMAIN = "example.com"
IGD_URL = "http://192.168.1.1:1900/igd.xml"
TTL = "300"


def label() -> str:
    return os.environ.get("DDNS_NAME", "ticket")


def _call(path: str, keys: dict, extra: dict | None = None) -> dict:
    # Same style as portal/app/dnssync._call (copied: that module imports PyYAML, this must stay stdlib-only).
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
    except ValueError:
        return {"status": "ERROR", "message": f"HTTP {code}, not JSON"}
    if not isinstance(doc, dict) or (not doc and code >= 400):
        return {"status": "ERROR", "message": f"HTTP {code}"}
    return doc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _redact(text: str, keys: dict) -> str:
    for v in keys.values():
        if v:
            text = text.replace(v, "[REDACTED]")
    return text


def _write_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ddns-")
    with os.fdopen(fd, "w") as f:
        json.dump(status, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _log(path: Path, line: str) -> None:
    try:
        with path.open("a") as f:
            f.write(f"{_now()} {line}\n")
    except OSError:
        pass  # simplified: logging is best-effort, ddns.json is the source of truth; upgrade: journald only


def run() -> dict:
    status_path = Path(os.environ.get("DDNS_STATUS", "/srv/homelab/collector/ddns.json"))
    log_path = Path(os.environ.get("DDNS_LOG", str(status_path.with_suffix(".log"))))
    fqdn = f"{label()}.{DOMAIN}"
    try:
        prev = json.loads(status_path.read_text())
    except (OSError, ValueError):
        prev = {}
    st = {"checked_at": _now(), "name": fqdn, "ok": False, "wan_ip": None, "dns_ip": None,
          "updated_at": prev.get("updated_at"), "error": None}
    keys = {"apikey": os.environ.get("PORKBUN_API_KEY", ""), "secretapikey": os.environ.get("PORKBUN_API_SECRET_KEY", "")}
    try:
        st["wan_ip"] = upnp.eero_status(IGD_URL).get("public_ip")
        if not st["wan_ip"]:
            st["error"] = "no WAN IP from Eero UPnP"
        elif not all(keys.values()):
            st["error"] = "Porkbun keys missing from environment"
        else:
            sub = label()
            got = _call(f"/dns/retrieveByNameType/{DOMAIN}/A/{sub}", keys)
            if got.get("status") != "SUCCESS":
                st["error"] = f"Porkbun retrieve failed: {got.get('message')}"
            else:
                recs = got.get("records") or []
                st["dns_ip"] = recs[0].get("content") if recs else None
                if st["dns_ip"] == st["wan_ip"]:
                    st["ok"] = True
                else:
                    if recs:
                        res = _call(f"/dns/editByNameType/{DOMAIN}/A/{sub}", keys, {"content": st["wan_ip"], "ttl": TTL})
                    else:
                        res = _call(f"/dns/create/{DOMAIN}", keys, {"name": sub, "type": "A", "content": st["wan_ip"], "ttl": TTL})
                    if res.get("status") == "SUCCESS":
                        _log(log_path, f"{fqdn}: {st['dns_ip']} -> {st['wan_ip']}")
                        st.update(ok=True, updated_at=_now(), dns_ip=st["wan_ip"])
                    else:
                        st["error"] = f"Porkbun update failed: {res.get('message')}"
    except Exception as e:  # a traceback could carry request data; keep only the type
        st["error"] = f"unexpected {type(e).__name__}"
    if st["error"]:
        st["error"] = _redact(str(st["error"]), keys)
        if st["error"] != prev.get("error"):
            _log(log_path, f"ERROR {st['error']}")
    _write_status(status_path, st)
    return st


def main() -> int:
    st = run()
    print("ok" if st["ok"] else f"error: {st['error']}")
    return 0  # simplified: always exit 0 so the timer never backs off; failures show in ddns.json


if __name__ == "__main__":
    sys.exit(main())
