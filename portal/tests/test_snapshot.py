import json
from datetime import datetime, timezone
from pathlib import Path

from app.snapshot import SnapshotBuilder, SnapshotStore, fmt_duration

TOPO = json.loads((Path(__file__).parents[1] / "app/topology.json").read_text("utf-8"))
HOST_OK = {"available": True, "cpu_pct": 30, "load": [1.2, 1.1, 1.0], "cores": 4,
           "mem": {"total_gb": 7.04, "used_gb": 2.0, "cache_gb": 4.0, "avail_gb": 5.04},
           "swap": {"total_gb": 4.0, "used_gb": 0.5}, "temps_c": [50.0],
           "disks": [{"mount": "/", "size_gb": 115.8, "used_gb": 40.0, "pct": 35}],
           "nics": [{"name": "enp2s0 (wired)", "rx_mbps": 2.0, "tx_mbps": 0.5, "rx_total_gb": 800.0, "tx_total_gb": 80.0}],
           "uptime_s": 90000}
EERO_OK = {"reachable": True, "connected": True, "uptime_s": 988131, "last_error": "ERROR_NONE", "public_ip": "203.0.113.7"}
DEPLOY_OK = {"commit": "abc1234", "message": "Portal slice 1", "at": "2026-10-01T09:00:00-04:00", "ok": True}
APPS = [{"id": "jobs", "lan_url": "http://192.168.4.5:8080"}]


def build(host=HOST_OK, eero=EERO_OK, deploy=DEPLOY_OK, apps=APPS):
    return SnapshotBuilder(TOPO, host=lambda: host, eero=lambda: eero, deploy=lambda: deploy, apps=lambda: apps,
                           now=lambda: datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)).build()


def node(snap, id):
    return next(n for n in snap["nodes"] if n["id"] == id)


def link(snap, id):
    return next(l for l in snap["links"] if l["id"] == id)


def test_live_host_values_replace_the_snapshot_values():
    snap = build()
    c = node(snap, "central")
    assert c["stats"]["cpu_pct"] == 30 and c["stats"]["mem"]["used_gb"] == 2.0
    assert c["meta"]["Uptime"] == "1 day 1 h"
    assert link(snap, "lan_central")["a_to_b_mbps"] == 2.0 and link(snap, "lan_central")["b_to_a_mbps"] == 0.5
    assert snap["ts"] == "2026-10-01T13:00:00Z" and "central" in snap["live"]


def test_topology_is_not_mutated_between_builds():
    build(host={**HOST_OK, "cpu_pct": 99})
    assert node(build(), "central")["stats"]["cpu_pct"] == 30


def test_host_unavailable_marks_central_unknown_without_failing():
    snap = build(host={"available": False, "error": "FileNotFoundError: loadavg"})
    c = node(snap, "central")
    assert c["status"] == "unknown" and "loadavg" in c["meta"]["Live stats"]


def test_memory_and_disk_thresholds_raise_alerts():
    hot = {**HOST_OK, "mem": {**HOST_OK["mem"], "used_gb": 6.3},
           "disks": [{"mount": "/", "size_gb": 100, "used_gb": 92, "pct": 92}]}
    kinds = {(a["target"], a["kind"], a["severity"]) for a in build(host=hot)["alerts"]}
    assert ("central", "mem_high", "warn") in kinds and ("central", "disk_high", "crit") in kinds


def test_eero_unreachable_is_a_critical_alert_on_the_router():
    snap = build(eero={"reachable": False, "error": "URLError"})
    assert node(snap, "router")["status"] == "crit"
    assert any(a["target"] == "router" and a["kind"] == "host_down" and a["severity"] == "crit" for a in snap["alerts"])


def test_internet_down_is_a_critical_alert_on_internet():
    snap = build(eero={**EERO_OK, "connected": False})
    assert any(a["target"] == "isp" and a["severity"] == "crit" for a in snap["alerts"])


def test_connected_eero_fills_internet_details():
    isp = node(build(), "isp")
    assert isp["status"] == "good" and isp["meta"]["Public IP"] == "203.0.113.7"
    assert isp["meta"]["Connection uptime"] == "11 days 10 h"


def test_failed_deploy_is_signal_failure_on_caddy():
    snap = build(deploy={**DEPLOY_OK, "ok": False})
    assert snap["edge"]["deploy"]["ok"] is False
    assert any(a["target"] == "caddy" and a["kind"] == "deploy_failed" for a in snap["alerts"])


def test_missing_deploy_log_keeps_topology_value():
    assert build(deploy=None)["edge"]["deploy"]["commit"] == TOPO["edge"]["deploy"]["commit"]


def test_app_links_come_from_the_registry():
    assert node(build(), "jobs")["url"] == "http://192.168.4.5:8080"


def test_broken_registry_does_not_break_the_snapshot():
    def bad():
        raise ValueError("apps.yaml entry jobs is missing: port")
    snap = SnapshotBuilder(TOPO, host=lambda: HOST_OK, eero=lambda: EERO_OK, deploy=lambda: DEPLOY_OK, apps=bad).build()
    assert node(snap, "central")["stats"]["cpu_pct"] == 30


def test_store_builds_once_then_caches():
    calls = []
    store = SnapshotStore(lambda: calls.append(1) or {"n": len(calls)})
    assert store.current() == {"n": 1} and store.current() == {"n": 1}
    assert store.refresh() == {"n": 2}


def test_fmt_duration():
    assert fmt_duration(90000) == "1 day 1 h" and fmt_duration(3600 * 50) == "2 days 2 h" and fmt_duration(59) == "0 h"


def _raises(exc):
    def f():
        raise exc
    return f


def test_any_source_failure_is_contained():
    import http.client
    import yaml
    snap = SnapshotBuilder(TOPO, host=_raises(RuntimeError("proc gone")), eero=_raises(http.client.IncompleteRead(b"")),
                           deploy=_raises(UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "bad")),
                           apps=_raises(yaml.YAMLError("bad yaml"))).build()
    assert node(snap, "central")["status"] == "unknown" and "proc gone" in node(snap, "central")["meta"]["Live stats"]
    assert node(snap, "router")["status"] == "crit"
    assert snap["edge"]["deploy"]["commit"] == TOPO["edge"]["deploy"]["commit"]
    assert snap["live"] == []


def test_links_use_https_names_and_dns_expectations_follow_the_registry():
    apps = [{"id": "jobs", "lan_url": "http://192.168.4.5:8080", "url": "https://jobs.hahbah.com",
             "fqdn": "jobs.hahbah.com", "edge_ip": "192.168.4.5", "host": "central"}]
    snap = build(apps=apps)
    assert node(snap, "jobs")["url"] == "https://jobs.hahbah.com"
    names = [e["name"] for e in snap["edge"]["dns"]["expected"]]
    assert names == ["home.hahbah.com", "jobs.hahbah.com"]


PLEX_OK = {"available": True, "live": {"stream_count": 2, "transcode_count": 1, "total_bandwidth_kbps": 12500, "streams": []},
           "totals": {"today": {"plays": 1, "hours": 0.5}, "week": {"plays": 3, "hours": 3.0}}, "top_users": [], "top_titles": []}


def test_plex_section_and_tautulli_station_when_available():
    snap = SnapshotBuilder(TOPO, host=lambda: HOST_OK, eero=lambda: EERO_OK, deploy=lambda: DEPLOY_OK, apps=lambda: APPS,
                           plex=lambda: PLEX_OK).build()
    assert snap["plex"]["live"]["stream_count"] == 2
    assert node(snap, "tautulli")["status"] == "good" and "tautulli" in snap["live"]
    assert link(snap, "app_tautulli")["a"] == "plex"


def test_tautulli_down_is_crit_and_not_set_up_is_unknown():
    down = SnapshotBuilder(TOPO, host=lambda: HOST_OK, eero=lambda: EERO_OK, deploy=lambda: DEPLOY_OK, apps=lambda: APPS,
                           plex=lambda: {"available": False, "reason": "Tautulli didn't answer"}).build()
    assert node(down, "tautulli")["status"] == "crit" and "tautulli" not in down["live"]
    assert node(down, "tautulli")["meta"]["Live stats"] == "unavailable (Tautulli didn't answer)"
    fresh = build()   # no plex source passed: defaults to "not set up"
    assert node(fresh, "tautulli")["status"] == "unknown" and fresh["plex"]["available"] is False


def test_plex_source_crash_is_contained():
    snap = SnapshotBuilder(TOPO, host=lambda: HOST_OK, eero=lambda: EERO_OK, deploy=lambda: DEPLOY_OK, apps=lambda: APPS,
                           plex=_raises(RuntimeError("boom"))).build()
    assert snap["plex"] == {"available": False, "reason": "RuntimeError"}


def test_admin_apps_are_marked_on_their_station():
    apps = APPS + [{"id": "tautulli", "name": "Tautulli", "subdomain": "tautulli", "host": "central", "port": 8181,
                    "health": "/status", "lan_url": "", "admin": True, "url": "https://tautulli.hahbah.com",
                    "fqdn": "tautulli.hahbah.com", "edge_ip": "192.168.4.5"}]
    snap = build(apps=apps)
    assert node(snap, "tautulli")["admin_only"] is True and node(snap, "tautulli")["url"] == "https://tautulli.hahbah.com"
    assert not node(snap, "jobs").get("admin_only")


def test_every_node_has_a_map_position_and_every_link_a_line():
    pos, lined = TOPO["layout"]["pos"], {l for ln in TOPO["layout"]["lines"] for l in ln["links"]}
    assert [n["id"] for n in TOPO["nodes"] if n["id"] not in pos] == []
    assert [l["id"] for l in TOPO["links"] if l["id"] not in lined] == []


def test_app_stations_keep_clear_of_machine_stations():
    """Machines get big buildings and long labels in 3D; an app parked next to one reads as part of it."""
    import math
    pos = TOPO["layout"]["pos"]
    machines = [n["id"] for n in TOPO["nodes"] if n["kind"] in ("host", "nas", "pc", "router", "gateway")]
    apps = [n["id"] for n in TOPO["nodes"] if n["kind"] == "app"]
    crowded = [(a, m, round(math.dist(pos[a], pos[m]))) for a in apps for m in machines if math.dist(pos[a], pos[m]) < 150]
    assert crowded == []


def build_live(cert=None, health=None, collector=None):
    return SnapshotBuilder(TOPO, host=lambda: HOST_OK, eero=lambda: EERO_OK, deploy=lambda: DEPLOY_OK, apps=lambda: APPS,
                           cert=lambda: cert, health=lambda: health or {}, collector=lambda: collector or {},
                           now=lambda: datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)).build()


def test_collector_sources_are_listed_with_their_age():
    snap = build_live(collector={"nas": {"ok": True, "checked_at": "x", "interval_s": 60, "age_s": 30, "stale": False,
                                         "data": {"stats": {"cpu_pct": 9, "load": [0.1, 0.1, 0.1], "cores": 2, "mem": {}, "swap": {},
                                                            "temps_c": [40], "disks": [], "nics": []},
                                                  "uptime_s": 3600, "raid": {"ok": True, "arrays": []}}}})
    assert node(snap, "nas")["stats"]["cpu_pct"] == 9 and "nas" in snap["live"]
    labels = {s["id"]: s for s in snap["sources"]}
    assert labels["nas"]["cadence"] == "every minute · checked 30 s ago"
    assert labels["edge"]["cadence"] == "every 6 h · not collected yet"


def test_one_bad_collector_document_does_not_break_the_snapshot():
    snap = build_live(collector={"nas": {"ok": True, "age_s": 5, "stale": False, "data": {"oops": 1}}},
                      health={"jobs": {"ok": True, "code": 200, "ms": 5.0}})
    assert node(snap, "jobs")["status"] == "good"   # health still applied after the NAS merge blew up
    assert node(snap, "nas")["status"] == "unknown"


def test_cert_and_health_feed_the_snapshot():
    snap = build_live(cert={"names": ["*.hahbah.com"], "issuer": "Let's Encrypt (YE1)", "issued": "2026-09-30T00:00:00Z",
                            "expires": "2026-12-29T00:00:00Z"},
                      health={"social": {"ok": True, "code": 200, "ms": 30.0}})
    assert snap["edge"]["cert"]["expires"] == "2026-12-29T00:00:00Z" and node(snap, "social")["status"] == "good"
    assert not [a for a in snap["alerts"] if a["id"] == "a2"]


def test_a_crashing_step_greys_that_steps_stations():
    bad = {"ok": True, "age_s": 5, "stale": False, "interval_s": 60, "data": {"/mnt/video": {}}}
    snap = build_live(collector={"mounts": bad, "smart": {"ok": False, "error": "x", "age_s": 5, "stale": False}})
    assert node(snap, "m_video")["status"] == "unknown" and node(snap, "sda")["status"] == "unknown"
