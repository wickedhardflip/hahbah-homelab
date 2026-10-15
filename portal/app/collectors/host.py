"""Central's own stats, read from the host's /proc and /sys mounted read-only into the container."""
import shutil
import time
from pathlib import Path

GIB_KB = 1048576          # kB in a GiB
GIB = 1024 ** 3


def _kb_fields(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if rest.strip():
            out[name.strip()] = int(rest.split()[0])
    return out


def read_memory(proc: Path) -> tuple:
    m = _kb_fields((proc / "meminfo").read_text())
    total = m["MemTotal"]
    avail = m.get("MemAvailable", m["MemFree"] + m.get("Buffers", 0) + m.get("Cached", 0))
    cache = m.get("Buffers", 0) + m.get("Cached", 0) + m.get("SReclaimable", 0)
    g = lambda kb: round(kb / GIB_KB, 2)
    mem = {"total_gb": g(total), "used_gb": g(total - avail), "cache_gb": g(cache), "avail_gb": g(avail)}
    swap = {"total_gb": g(m.get("SwapTotal", 0)), "used_gb": g(m.get("SwapTotal", 0) - m.get("SwapFree", 0))}
    return mem, swap


def read_net(proc: Path, nic: str) -> tuple:
    """(rx_bytes, tx_bytes). /proc/1/net/dev is the host's network namespace, not the container's."""
    path = proc / "1/net/dev"
    for line in (path if path.exists() else proc / "net/dev").read_text().splitlines():
        name, _, rest = line.partition(":")
        if name.strip() == nic:
            f = rest.split()
            return int(f[0]), int(f[8])
    raise ValueError(f"interface {nic} not found")


def read_temps(sys: Path) -> list:
    temps = []
    for zone in sorted((sys / "class/thermal").glob("thermal_zone*/temp")):
        try:
            temps.append(round(int(zone.read_text().strip()) / 1000, 1))
        except (OSError, ValueError):
            continue
    return temps


class HostSampler:
    def __init__(self, proc: Path, sys: Path, nic: str, disks: tuple, clock=time.monotonic):
        self.proc, self.sys, self.nic, self.disks, self.clock = Path(proc), Path(sys), nic, disks, clock
        self._last_net = None     # (time, rx, tx)

    def sample(self) -> dict:
        try:
            return {"available": True, **self._read()}
        except (OSError, ValueError, KeyError, IndexError) as e:
            return {"available": False, "error": f"{type(e).__name__}: {e}"}

    def _read(self) -> dict:
        load = [float(x) for x in (self.proc / "loadavg").read_text().split()[:3]]
        cores = sum(1 for l in (self.proc / "cpuinfo").read_text().splitlines() if l.startswith("processor")) or 1
        mem, swap = read_memory(self.proc)
        now, (rx, tx) = self.clock(), read_net(self.proc, self.nic)
        rx_mbps = tx_mbps = None
        if self._last_net and now > self._last_net[0]:
            dt = now - self._last_net[0]
            rx_mbps = round((rx - self._last_net[1]) * 8 / dt / 1e6, 3)
            tx_mbps = round((tx - self._last_net[2]) * 8 / dt / 1e6, 3)
        self._last_net = (now, rx, tx)
        disks = []
        for label, path in self.disks:
            u = shutil.disk_usage(path)
            disks.append({"mount": label, "size_gb": round(u.total / GIB, 2), "used_gb": round(u.used / GIB, 2),
                          "pct": round(u.used / u.total * 100) if u.total else 0})
        return {
            "cpu_pct": min(100, round(load[0] / cores * 100)), "load": load, "cores": cores,
            "mem": mem, "swap": swap, "temps_c": read_temps(self.sys), "disks": disks,
            "nics": [{"name": f"{self.nic} (wired)", "rx_mbps": rx_mbps, "tx_mbps": tx_mbps,
                      "rx_total_gb": round(rx / GIB, 1), "tx_total_gb": round(tx / GIB, 1)}],
            "uptime_s": float((self.proc / "uptime").read_text().split()[0]),
        }
