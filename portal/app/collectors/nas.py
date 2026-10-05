"""Synology NAS stats from one SSH command (the NAS key may run nothing else).

The command prints /proc files and a few extras between ==name== markers; see deploy/nas-collector-key.sh.
CPU and network rates need two samples, so a NasSampler keeps the previous counters between polls.
"""
import re
import subprocess

GIB_KB = 1024 * 1024
GIB = 1024 ** 3
NIC = "eth1"   # the DS416play's cabled port (eth0 is unplugged)
MARK = re.compile(r"^==(\w+)==$", re.M)
MD_HEAD = re.compile(r"^(md\d+) : (\w+) (raid\d+|linear)", re.M)
MD_STATE = re.compile(r"\[(\d+)/(\d+)\] \[([U_]+)\]")

NAS_COMMAND = ("echo ==stat==; cat /proc/stat; echo ==meminfo==; cat /proc/meminfo; echo ==loadavg==; cat /proc/loadavg; "
               "echo ==uptime==; cat /proc/uptime; echo ==mdstat==; cat /proc/mdstat; echo ==netdev==; cat /proc/net/dev; "
               "echo ==df==; df -k /volume1; echo ==temps==; for f in /sys/class/hwmon/hwmon*/temp*_input "
               "/sys/class/hwmon/hwmon*/device/temp*_input /sys/class/thermal/thermal_zone*/temp; do "
               "[ -r \"$f\" ] && echo \"$f $(cat $f)\"; done; echo ==end==")


def parse_sections(raw: str) -> dict:
    parts = MARK.split(raw)
    return {parts[i]: parts[i + 1].strip("\n") for i in range(1, len(parts) - 1, 2)}


def parse_mdstat(text: str) -> dict:
    arrays = []
    for block in re.split(r"\n(?=md\d+ :)", text):
        head, state = MD_HEAD.search(block), MD_STATE.search(block)
        if head and state:
            ok = state.group(1) == state.group(2) and "_" not in state.group(3)
            arrays.append({"name": head.group(1), "level": head.group(3), "state": state.group(3), "ok": ok})
    return {"ok": bool(arrays) and all(a["ok"] for a in arrays), "arrays": arrays}


def _kb(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        if v.strip():
            out[k.strip()] = int(v.split()[0])
    return out


def _memory(text: str) -> tuple:
    m = _kb(text)
    total, cache = m["MemTotal"], m.get("Cached", 0) + m.get("Buffers", 0)
    avail = m.get("MemAvailable", m["MemFree"] + cache)
    g = lambda kb: round(kb / GIB_KB, 2)
    return ({"total_gb": g(total), "used_gb": g(total - avail), "cache_gb": g(cache), "avail_gb": g(avail)},
            {"total_gb": g(m.get("SwapTotal", 0)), "used_gb": g(m.get("SwapTotal", 0) - m.get("SwapFree", 0))})


def _cpu(text: str) -> tuple:
    """(busy jiffies, total jiffies, core count) from /proc/stat."""
    first = text.splitlines()[0].split()[1:]
    v = [int(x) for x in first]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    cores = sum(1 for l in text.splitlines() if re.match(r"cpu\d+ ", l))
    return sum(v) - idle, sum(v), cores


def _net(text: str) -> tuple:
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name.strip() == NIC:
            f = rest.split()
            return int(f[0]), int(f[8])
    return None


def _df(text: str) -> list:
    out = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 6:
            size, used = int(f[1]) * 1024, int(f[2]) * 1024
            out.append({"mount": f[5], "size_gb": round(size / GIB, 2), "used_gb": round(used / GIB, 2),
                        "pct": int(f[4].rstrip("%")) if f[4].rstrip("%").isdigit() else round(used / size * 100)})   # df's own Use%
    return out


def _temps(text: str) -> list:
    temps = []
    for line in text.splitlines():
        f = line.split()
        if len(f) == 2 and f[1].lstrip("-").isdigit() and int(f[1]) > 0:
            temps.append(round(int(f[1]) / 1000))
    return temps


class NasSampler:
    def __init__(self):
        self._prev = None   # (now, busy, total, rx, tx)

    def sample(self, raw: str, now: float) -> dict:
        s = parse_sections(raw)
        mem, swap = _memory(s["meminfo"])
        busy, total, cores = _cpu(s["stat"])
        net = _net(s.get("netdev", ""))
        cpu_pct = rx_mbps = tx_mbps = None
        if self._prev:
            p_now, p_busy, p_total, p_rx, p_tx = self._prev
            if total > p_total:
                cpu_pct = round((busy - p_busy) / (total - p_total) * 100)
            dt = now - p_now
            if net and p_rx is not None and dt > 0:
                rx_mbps = round(max(0, net[0] - p_rx) * 8 / dt / 1e6, 3)
                tx_mbps = round(max(0, net[1] - p_tx) * 8 / dt / 1e6, 3)
        self._prev = (now, busy, total, net[0] if net else None, net[1] if net else None)
        load = [float(x) for x in s["loadavg"].split()[:3]]
        nics = [{"name": f"{NIC} (wired)", "rx_mbps": rx_mbps, "tx_mbps": tx_mbps,
                 "rx_total_gb": round(net[0] / GIB, 1), "tx_total_gb": round(net[1] / GIB, 1)}] if net else []
        return {"stats": {"cpu_pct": cpu_pct, "load": load, "cores": cores, "mem": mem, "swap": swap,
                          "temps_c": _temps(s.get("temps", "")), "disks": _df(s.get("df", "")), "nics": nics},
                "uptime_s": int(float(s["uptime"].split()[0])), "raid": parse_mdstat(s.get("mdstat", ""))}


# The script the NAS's authorized_keys line forces for the collector key (installed at ~/bin/homelab-stats.sh).
# It reads the requested word from SSH_ORIGINAL_COMMAND: "smart" prints the drives' SMART data, anything else the stats.
NAS_SCRIPT = "\n".join([
    "#!/bin/sh",
    "# homelab collector: read-only NAS stats (the only thing the homelab-collector key may run)",
    'if [ "$SSH_ORIGINAL_COMMAND" = "smart" ]; then',
    '  for d in sda sdb sdc sdd; do echo "==drive $d=="; /usr/syno/bin/synodisk --info /dev/$d; echo ===SMART===; '
    "/usr/syno/bin/synodisk --smart_info_get /dev/$d; done; echo ==end==; exit 0",
    "fi",
    NAS_COMMAND,
    "",
])


def run_nas_command(host: str, user: str, key: str, known_hosts: str, timeout: float = 15.0, command: str = "stats") -> str:
    """The NAS's forced command only understands "stats" (default) and "smart"; it can't run anything else."""
    out = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                          "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8", f"{user}@{host}", command],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0 or "==end==" not in out.stdout:
        raise RuntimeError(f"ssh exit {out.returncode}: {out.stderr.strip()[-200:]}")
    return out.stdout
