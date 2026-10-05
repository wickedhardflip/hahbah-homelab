"""The checks LegacyMonitor used to run, now in the collector: NFS mounts, pings, drive SMART (via the NAS's restricted
stats key) and the daily internet speed test. Outputs keep the shapes the snapshot merge already understands."""
import json
import os
import re
import signal
import subprocess
from datetime import datetime

PING_TIME = re.compile(r"time[=<]([\d.]+)\s*ms")


def _z(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def default_run(argv: list, timeout: float) -> tuple:
    """(returncode, stdout). A command that outlives `timeout` counts as failed (124), never as a hang."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""


# ---------- NFS mounts ----------
# One child does the whole check, so a hung NFS stat/readdir can only ever block that child. Exit 3 = not mounted.
MOUNT_PROBE = 'mountpoint -q "$1" || exit 3; ls -1 "$1" >/dev/null 2>&1'


def run_detached(argv: list, timeout: float) -> int:
    """Return code of a child with no pipes, in its own process group. On timeout the group is killed and the child is
    abandoned (a process stuck on a dead NFS server can't be reaped), so the caller never waits more than ~timeout."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError:
        return 127
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return 124


def check_mounts(paths: list, root: str, now: datetime, run=run_detached) -> dict:
    """`root` is where the host's / is bind-mounted for /mnt (rslave) inside the container; paths are the host paths."""
    if not os.path.isdir(os.path.join(root, "mnt")):
        raise FileNotFoundError(f"{root}/mnt is not a directory (check MOUNTS_ROOT)")
    out = {}
    for p in paths:
        local = os.path.join(root, p.lstrip("/"))
        rc = run(["sh", "-c", MOUNT_PROBE, "probe", local], 8)
        out[p] = {"mounted": rc != 3, "responsive": rc == 0, "checked_at": _z(now)}
    return out


# ---------- pings ----------
def parse_ping(code: int, stdout: str) -> dict:
    m = PING_TIME.search(stdout or "")
    if code == 0 and m:
        return {"reachable": True, "latency_ms": round(float(m.group(1)), 3)}
    return {"reachable": False, "latency_ms": None}


def ping_hosts(targets: dict, now: datetime, run=default_run) -> dict:
    return {name: {**parse_ping(*run(["ping", "-c", "1", "-W", "2", host], 4)), "checked_at": _z(now)}
            for name, host in targets.items()}


# ---------- drive SMART (synodisk output, same parsing rules LegacyMonitor used) ----------
def parse_smart(text: str) -> dict:
    info, _, smart = text.partition("===SMART===")
    res = {"model": "Unknown", "health": "healthy", "temp_c": None, "reallocated": None, "power_on_hours": None}
    for line in info.splitlines():
        if "Disk model:" in line:
            res["model"] = line.split(":", 1)[1].strip()
    for block in smart.split("---------------------"):
        fields = {}
        for line in block.strip().splitlines():
            if ": " in line:
                k, v = line.split(": ", 1)
                fields[k.strip()] = v.strip()
        try:
            attr, raw = int(fields.get("Id", 0)), int(fields.get("Raw", "0").split()[0])
        except ValueError:
            continue
        status = fields.get("Status", "OK")
        if attr == 194:
            res["temp_c"] = raw
        elif attr == 5:
            res["reallocated"] = raw
            if status not in ("OK", ""):
                res["health"] = "failing"
            elif raw > 0 and res["health"] == "healthy":
                res["health"] = "warning"
        elif attr == 9:
            res["power_on_hours"] = raw
        if "FAILING" in status.upper():
            res["health"] = "failing"
    return res


def split_drives(stdout: str) -> dict:
    """The NAS 'smart' command prints ==drive sdX== before each drive's synodisk output."""
    parts = re.split(r"^==drive (\w+)==$", stdout, flags=re.M)
    return {parts[i]: parts[i + 1].split("==end==")[0] for i in range(1, len(parts) - 1, 2)}


def smart_all(stdout: str, now: datetime) -> dict:
    out = {}
    for name, text in split_drives(stdout).items():
        if "===SMART===" in text:
            out[name] = {**parse_smart(text), "checked_at": _z(now)}
    if not out:
        raise RuntimeError("no drive data in the NAS answer")
    return out


# ---------- internet speed test ----------
def parse_speedtest(stdout: str, now: datetime) -> dict:
    j = json.loads(stdout)
    return {"download_mbps": round(j["download"] / 1e6, 1), "upload_mbps": round(j["upload"] / 1e6, 1),
            "latency_ms": round(float(j["ping"]), 1), "tested_at": _z(now)}


def speed_test(now: datetime, run=default_run) -> dict:
    code, out = run(["speedtest-cli", "--json", "--secure"], 120)
    if code != 0 or not out.strip():
        raise RuntimeError(f"speedtest-cli exit {code}")
    return parse_speedtest(out, now)
