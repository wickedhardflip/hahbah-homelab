"""The last line (and, on request, the last 10 parsed lines) of /srv/homelab/deploy.log, written by deploy/pull.sh."""
import re
from pathlib import Path

LINE = re.compile(r"^(\S+) (deployed|FAILED (?:config check|build|firewall|caddy reload) at) ([0-9a-f]{7,40})(?: v([0-9][0-9.]*))? \((.*)\)$")


def _parsed(m) -> dict:
    at, kind, commit, version, message = m.groups()
    return {"commit": commit, "message": message, "at": at, "ok": kind == "deployed", **({"version": version} if version else {})}


def last_deploy(path: Path, history: bool = False) -> dict | None:
    try:
        lines = [l for l in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except OSError:
        return None
    if not lines:
        return None
    m = LINE.match(lines[-1].strip())
    if not m:
        return {"commit": "?", "message": f"Unreadable deploy log line: {lines[-1][:80]}", "at": None, "ok": False}
    out = _parsed(m)
    if history:   # newest first; lines that don't parse are skipped, not guessed at
        out["history"] = [_parsed(x) for x in (LINE.match(l.strip()) for l in reversed(lines)) if x][:10]
    return out
