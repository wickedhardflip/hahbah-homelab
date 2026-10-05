"""The app list (apps.yaml): one entry per app, shared by the dashboard, nav, Caddy and DNS."""
import copy
import threading
from pathlib import Path

import yaml

REQUIRED = ("id", "name", "subdomain", "host", "port", "health", "lan_url")


_cache: dict = {}   # path -> (mtime_ns, size, parsed site); apps.yaml is re-read only when the file changes
_lock = threading.Lock()


def load_site(path: Path) -> dict:
    """Parsed apps.yaml. Callers get their own copy, so changing it can't leak into the next request."""
    try:
        st = Path(path).stat()
    except OSError:
        st = None
    key = (st.st_mtime_ns, st.st_size) if st else None
    with _lock:
        hit = _cache.get(str(path))
    if key and hit and hit[0] == key:
        return copy.deepcopy(hit[1])
    site = _parse_site(path)
    if key:
        with _lock:
            _cache[str(path)] = (key, site)
    return copy.deepcopy(site)


def _parse_site(path: Path) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {"domain": None, "edge_host": None, "hosts": {}, "apps": []}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"apps.yaml isn't valid YAML: {e}") from e
    domain, edge, hosts = data.get("domain"), data.get("edge_host"), data.get("hosts") or {}
    apps = data.get("apps") or []
    for app in apps:
        missing = [k for k in REQUIRED if k not in app]
        if missing:
            raise ValueError(f"apps.yaml entry {app.get('id', '?')} is missing: {', '.join(missing)}")
        if not app["lan_url"] and not app.get("upstream"):
            raise ValueError(f"apps.yaml entry {app['id']} has no lan_url and no upstream")
        if hosts and app["host"] not in hosts:
            raise ValueError(f"apps.yaml entry {app['id']} runs on unknown host {app['host']}")
        named = bool(domain and edge and edge in hosts)
        app["fqdn"] = f"{app['subdomain']}.{domain}" if named else None
        app["url"] = f"https://{app['fqdn']}" if named else app["lan_url"]
        app["edge_ip"] = hosts[edge]["ip"] if named else None
    return {"domain": domain, "edge_host": edge, "hosts": hosts, "apps": apps}


def load_registry(path: Path) -> list:
    return load_site(path)["apps"]
