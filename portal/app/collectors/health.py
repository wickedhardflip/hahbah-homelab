"""HTTP health checks for every app in apps.yaml, plus Ollama on the PC. All run in parallel with a short timeout."""
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect (e.g. to a login page) already proves the app answered


_opener = urllib.request.build_opener(_NoRedirect)


def default_get(url: str, timeout: float) -> tuple:
    try:
        with _opener.open(url, timeout=timeout) as r:
            return r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        if e.code < 500:
            return e.code, b""
        raise


def health_targets(apps: list, gateway: str, ollama_url: str) -> list:
    out = []
    for a in apps:
        if not a.get("health"):
            continue
        base = f"http://{a['upstream']}" if a.get("upstream") else f"http://{gateway}:{a['port']}"
        out.append((a["id"], base + a["health"]))
    if ollama_url:
        out.append(("ollama", ollama_url.rstrip("/") + "/api/tags"))
    return out


def _one(name: str, url: str, get, timeout: float) -> dict:
    t0 = time.monotonic()
    try:
        code, body = get(url, timeout)
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": e.code, "ms": round((time.monotonic() - t0) * 1000, 1), "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 (any failure means "down"; the class name is the reason shown)
        return {"ok": False, "code": None, "ms": round((time.monotonic() - t0) * 1000, 1), "error": type(e).__name__}
    ok = code < 400 or code in (401, 403)
    res = {"ok": ok, "code": code, "ms": round((time.monotonic() - t0) * 1000, 1)}
    if not ok:
        res["error"] = f"HTTP {code}"
    if name == "ollama" and body:
        try:
            res["models"] = [m["name"] for m in json.loads(body).get("models", [])]
        except (ValueError, KeyError, TypeError):
            pass
    return res


def check_all(targets: list, get=default_get, timeout: float = 2.5) -> dict:
    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=min(12, len(targets))) as pool:
        futures = {name: pool.submit(_one, name, url, get, timeout) for name, url in targets}
        return {name: f.result() for name, f in futures.items()}
