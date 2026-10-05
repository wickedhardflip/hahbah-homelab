"""The last line of /srv/homelab/deploy.log, written by deploy/pull.sh."""
import re
from pathlib import Path

LINE = re.compile(r"^(\S+) (deployed|FAILED (?:config check|build|firewall|caddy reload) at) ([0-9a-f]{7,40}) \((.*)\)$")


def last_deploy(path: Path) -> dict | None:
    try:
        lines = [l for l in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except OSError:
        return None
    if not lines:
        return None
    m = LINE.match(lines[-1].strip())
    if not m:
        return {"commit": "?", "message": f"Unreadable deploy log line: {lines[-1][:80]}", "at": None, "ok": False}
    at, kind, commit, message = m.groups()
    return {"commit": commit, "message": message, "at": at, "ok": kind == "deployed"}
