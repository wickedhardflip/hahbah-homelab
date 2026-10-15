#!/usr/bin/env python3
"""WireGuard status writer for the HAHBAH portal. Stdlib only; runs as root every minute (wgstatus.timer).

Reads `wg show <iface> dump` and the kernel log, keeps connection + knock history, and writes one JSON file the
portal's collector directory serves:  /srv/homelab/collector/wireguard.json  (env WG_STATUS)
History lives in /srv/homelab/wireguard/history.json  (env WG_HISTORY).

Secrets: the dump contains the interface private key and each peer's preshared key. Those fields are never stored,
logged or written; peers are known by the name in wg0.conf ("# peer: <name>"), or the first 8 characters of the public key.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

IFACE = os.environ.get("WG_IFACE", "wg0")
CONF = os.environ.get("WG_CONF", f"/etc/wireguard/{IFACE}.conf")
HISTORY = os.environ.get("WG_HISTORY", "/srv/homelab/wireguard/history.json")
STATUS = os.environ.get("WG_STATUS", "/srv/homelab/collector/wireguard.json")

CONNECTED_S = 180          # a peer is connected when its latest handshake is younger than this; a bigger gap starts a new session
KEEP_DAYS = 90             # sessions older than this are dropped
KNOCK_DAYS = 30
MAX_SESSIONS_OUT = 50
TOP_SOURCES = 10
MAX_SOURCES = 2000         # simplified: a spoofed-source flood is capped to the busiest 2000 IPs; upgrade: a rotating sqlite table
MAX_WINDOW_S = 600         # never ask the journal for more than 10 min at once

# A kernel log line (dynamic debug on) rejecting a packet: "wireguard: wg0: Invalid MAC of handshake, dropping packet from 1.2.3.4:5678".
# IPv6 sources are bracketed: "from [2001:db8::1]:5678". Verify against the real log format on the server.
KNOCK_RE = re.compile(r"wireguard.*?(?:invalid|dropping packet).*?\bfrom\s+(?P<ip>\[[0-9a-f:.%]+\]|\d{1,3}(?:\.\d{1,3}){3}):\d+",
                      re.IGNORECASE)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def endpoint_ip(endpoint: str):
    """'1.2.3.4:5' -> '1.2.3.4'; '[::1]:5' -> '::1'; '(none)' -> None. The port is dropped."""
    if not endpoint or endpoint == "(none)":
        return None
    return endpoint.rpartition(":")[0].strip("[]") or None


def parse_dump(text: str):
    """(listen_port or None, [peer dicts]). The private and preshared keys are read past, never kept."""
    lines = [l.split("\t") for l in text.splitlines() if l.strip()]
    if not lines:
        return None, []
    iface, peers = lines[0], []
    port = int(iface[2]) if len(iface) > 2 and iface[2].isdigit() else None
    for f in lines[1:]:
        if len(f) < 7:
            continue
        peers.append({"pub": f[0], "endpoint_ip": endpoint_ip(f[2]), "handshake": int(f[4] or 0), "rx": int(f[5] or 0), "tx": int(f[6] or 0)})
    return port, peers


def parse_names(conf: str) -> dict:
    """public key -> name, from `# peer: <name>` comments above each [Peer] block. Only PublicKey lines are read."""
    names, pending = {}, None
    for line in conf.splitlines():
        line = line.strip()
        m = re.match(r"#\s*peer:\s*(.+)$", line, re.IGNORECASE)
        if m:
            pending = m.group(1).strip()
            continue
        m = re.match(r"PublicKey\s*=\s*(\S+)", line, re.IGNORECASE)
        if m:
            if pending:
                names[m.group(1)] = pending
            pending = None
    return names


def count_knocks(log_text: str) -> dict:
    """source IP -> number of rejected packets in these journal lines."""
    out: dict = {}
    for line in log_text.splitlines():
        m = KNOCK_RE.search(line)
        if m:
            ip = m.group("ip").strip("[]")
            out[ip] = out.get(ip, 0) + 1
    return out


def update_sessions(hist: dict, name: str, p: dict, now: float) -> None:
    """Open a session on a new handshake after a >= 180 s gap; otherwise extend the peer's current one."""
    state = hist.setdefault("peers", {})
    prev = state.get(name)
    hs = p["handshake"]
    rx0, tx0 = (prev["rx"], prev["tx"]) if prev else (0, 0)
    d_rx = p["rx"] - rx0 if p["rx"] >= rx0 else p["rx"]    # counters restart when the interface does
    d_tx = p["tx"] - tx0 if p["tx"] >= tx0 else p["tx"]
    state[name] = {"hs": hs, "rx": p["rx"], "tx": p["tx"]}
    if not hs:
        return
    sessions = hist.setdefault("sessions", [])
    cur = next((s for s in reversed(sessions) if s["peer"] == name), None)
    prev_hs = prev["hs"] if prev else 0
    if cur is None or (hs != prev_hs and hs - prev_hs >= CONNECTED_S):
        cur = {"peer": name, "start": iso(hs), "last_seen": iso(hs), "endpoint_ip": p["endpoint_ip"], "rx_bytes": 0, "tx_bytes": 0}
        sessions.append(cur)
    cur["rx_bytes"] += d_rx
    cur["tx_bytes"] += d_tx
    if p["endpoint_ip"]:
        cur["endpoint_ip"] = p["endpoint_ip"]
    cur["last_seen"] = iso(now) if now - hs < CONNECTED_S else max(cur["last_seen"], iso(hs))


def update_knocks(hist: dict, counts: dict, now: float, day: str) -> None:
    k = hist.setdefault("knocks", {"daily": {}, "sources": {}})
    for ip, n in counts.items():
        k["daily"][day] = k["daily"].get(day, 0) + n
        src = k["sources"].setdefault(ip, {"days": {}, "last_seen": iso(now)})
        src["days"][day] = src["days"].get(day, 0) + n
        src["last_seen"] = iso(now)


def prune(hist: dict, now: float, today) -> None:
    cut = iso(now - KEEP_DAYS * 86400)
    hist["sessions"] = [s for s in hist.get("sessions", []) if s["last_seen"] >= cut]
    k = hist.get("knocks")
    if not k:
        return
    keep = {(today - timedelta(days=i)).isoformat() for i in range(KNOCK_DAYS)}
    k["daily"] = {d: n for d, n in k["daily"].items() if d in keep}
    for src in k["sources"].values():
        src["days"] = {d: n for d, n in src["days"].items() if d in keep}
    live = {ip: s for ip, s in k["sources"].items() if s["days"]}
    top = sorted(live, key=lambda ip: -sum(live[ip]["days"].values()))[:MAX_SOURCES]
    k["sources"] = {ip: live[ip] for ip in top}


def knocks_summary(hist: dict, today) -> dict:
    k = hist.get("knocks") or {"daily": {}, "sources": {}}
    days = [(today - timedelta(days=i)).isoformat() for i in range(KNOCK_DAYS - 1, -1, -1)]
    top = sorted(k["sources"].items(), key=lambda kv: -sum(kv[1]["days"].values()))[:TOP_SOURCES]
    return {"today": k["daily"].get(today.isoformat(), 0), "daily": [{"date": d, "count": k["daily"].get(d, 0)} for d in days],
            "top_sources": [{"ip": ip, "count": sum(s["days"].values()), "last_seen": s["last_seen"]} for ip, s in top]}


def build_status(hist: dict, port, peers: list, names: dict, up: bool, now: float, today) -> dict:
    out_peers = []
    for p in peers:
        hs = p["handshake"]
        out_peers.append({"name": names.get(p["pub"]) or p["pub"][:8], "connected": bool(hs) and now - hs < CONNECTED_S,
                          "last_handshake": iso(hs) if hs else None, "rx": p["rx"], "tx": p["tx"], "endpoint_ip": p["endpoint_ip"]})
    sessions = sorted(hist.get("sessions", []), key=lambda s: s["start"], reverse=True)[:MAX_SESSIONS_OUT]
    # ok + interval_s are what the portal's collector reader expects of every file in that folder
    return {"ok": True, "interval_s": 60, "checked_at": iso(now), "interface_up": up, "listen_port": port, "peers": out_peers,
            "sessions": sessions, "knocks": knocks_summary(hist, today)}


def write_atomic(path: str, doc: dict, mode: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_history(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            h = json.load(f)
        return h if isinstance(h, dict) else {}
    except (OSError, ValueError):
        return {}


def run(cmd: list, timeout: float):
    """stdout on success, None on any failure (missing binary, non-zero exit, timeout)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def main(now: float | None = None) -> int:
    now = now if now is not None else time.time()
    today = datetime.fromtimestamp(now).astimezone().date()   # the box's local day
    hist = load_history(HISTORY)
    dump = run(["wg", "show", IFACE, "dump"], 10)
    up = bool(dump and dump.strip())
    port, peers, names = None, [], {}
    if up:
        port, peers = parse_dump(dump)
        try:
            with open(CONF, encoding="utf-8") as f:
                names = parse_names(f.read())
        except OSError:
            pass
        for p in peers:
            update_sessions(hist, names.get(p["pub"]) or p["pub"][:8], p, now)
    # Knocks: journal lines in (previous run, this run] at one-second resolution, so no line is counted twice.
    until = int(now)
    since = max(min(int(hist.get("knocks_until", until - 60)) + 1, until), until - MAX_WINDOW_S)
    fmt = lambda e: datetime.fromtimestamp(e).strftime("%Y-%m-%d %H:%M:%S")
    log = run(["journalctl", "-k", "--since", fmt(since), "--until", fmt(until), "-o", "cat", "--no-pager"], 20)
    if log is not None:   # a failed read keeps the old marker, so the next run retries the same window
        update_knocks(hist, count_knocks(log), now, today.isoformat())
        hist["knocks_until"] = until
    prune(hist, now, today)
    write_atomic(HISTORY, hist, 0o600)
    write_atomic(STATUS, build_status(hist, port, peers, names, up, now, today), 0o644)
    return 0


if __name__ == "__main__":
    sys.exit(main())
