from pathlib import Path

from app.collectors.nas import NasSampler, parse_mdstat, parse_sections

RAW = (Path(__file__).parent / "fixtures/collector/nas_raw.txt").read_text(encoding="utf-8")


def bump_cpu(raw, busy, idle):
    """The same output a minute later, with `busy` more user jiffies and `idle` more idle jiffies."""
    head, rest = raw.split("\ncpu ", 1)
    nums, tail = rest.split("\n", 1)
    v = [int(x) for x in nums.split()]
    v[0] += busy
    v[3] += idle
    return head + "\ncpu  " + " ".join(map(str, v)) + "\n" + tail


def test_sections_split_on_markers():
    s = parse_sections(RAW)
    assert {"stat", "meminfo", "loadavg", "uptime", "mdstat", "netdev", "df", "temps"} <= set(s)


def test_first_sample_has_everything_but_rates():
    out = NasSampler().sample(RAW, now=1000.0)
    st = out["stats"]
    assert st["cpu_pct"] is None and st["cores"] == 2
    assert st["load"] == [0.16, 0.13, 0.08]
    d = st["disks"][0]
    assert d["mount"] == "/volume1" and d["pct"] == 57 and abs(d["size_gb"] - 10717.7) < 0.1 and abs(d["used_gb"] - 6049.8) < 0.1
    assert st["temps_c"] == [44, 42]
    assert 7 < st["mem"]["total_gb"] < 8.5 and st["mem"]["used_gb"] > 0
    assert out["uptime_s"] == 2105816
    assert out["raid"] == {"ok": True, "arrays": [{"name": "md2", "level": "raid5", "state": "UUUU", "ok": True},
                                                   {"name": "md1", "level": "raid1", "state": "UUUU", "ok": True},
                                                   {"name": "md0", "level": "raid1", "state": "UUUU", "ok": True}]}


def test_second_sample_gives_cpu_and_network_rates():
    s = NasSampler()
    s.sample(RAW, now=1000.0)
    out = s.sample(bump_cpu(RAW, busy=30, idle=70), now=1060.0)
    assert out["stats"]["cpu_pct"] == 30
    nic = out["stats"]["nics"][0]
    assert nic["name"] == "eth1 (wired)" and nic["rx_mbps"] == 0.0 and nic["rx_total_gb"] > 100


def test_degraded_raid_is_reported():
    md = "md2 : active raid5 sda5[0] sdd5[3] sdc5[2]\n      11706562368 blocks super 1.2 level 5, 64k chunk, algorithm 2 [4/3] [UUU_]\n"
    r = parse_mdstat(md)
    assert r["ok"] is False and r["arrays"][0]["state"] == "UUU_"
