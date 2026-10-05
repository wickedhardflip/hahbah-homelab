import shutil
from pathlib import Path

from app.collectors.host import HostSampler

FIX = Path(__file__).parent / "fixtures"


def sampler(tmp_path, clock):
    proc = tmp_path / "proc"
    shutil.copytree(FIX / "proc", proc)
    return HostSampler(proc, FIX / "sys", "enp2s0", (("/", str(tmp_path)),), clock=clock), proc


def test_reads_load_memory_swap_temps_and_disk(tmp_path):
    s, _ = sampler(tmp_path, clock=lambda: 0.0)
    snap = s.sample()
    assert snap["available"] is True
    assert snap["cores"] == 4 and snap["load"] == [1.03, 1.04, 1.0]
    assert snap["cpu_pct"] == 26                       # 1.03 / 4 cores
    assert snap["mem"]["total_gb"] == 7.04 and snap["mem"]["used_gb"] == 1.04   # total - available
    assert snap["swap"] == {"total_gb": 4.0, "used_gb": 0.87}
    assert snap["temps_c"] == [52.0, 27.8]
    assert snap["disks"][0]["mount"] == "/" and 0 <= snap["disks"][0]["pct"] <= 100
    assert snap["uptime_s"] == 1919234.67


def test_network_rates_come_from_the_second_sample(tmp_path):
    t = [0.0]
    s, proc = sampler(tmp_path, clock=lambda: t[0])
    first = s.sample()["nics"][0]
    assert first["rx_mbps"] is None and first["rx_total_gb"] == 746.1
    dev = (proc / "1/net/dev").read_text().replace("801111100000", "801112350000")  # +1,250,000 bytes
    (proc / "1/net/dev").write_text(dev)
    t[0] = 10.0
    second = s.sample()["nics"][0]
    assert second["rx_mbps"] == 1.0 and second["tx_mbps"] == 0.0   # 1.25 MB in 10 s = 1 Mbps


def test_missing_proc_is_reported_not_raised(tmp_path):
    s = HostSampler(tmp_path / "nope", tmp_path / "nope", "enp2s0", ())
    snap = s.sample()
    assert snap["available"] is False
    assert "loadavg" in snap["error"]


def test_meminfo_without_memavailable_uses_free_buffers_cached(tmp_path):
    s, proc = sampler(tmp_path, clock=lambda: 0.0)
    lines = [l for l in (proc / "meminfo").read_text().splitlines() if not l.startswith("MemAvailable")]
    (proc / "meminfo").write_text("\n".join(lines))
    mem = s.sample()["mem"]
    assert mem["avail_gb"] == round((2781184 + 10240 + 3000000) / 1048576, 2)
