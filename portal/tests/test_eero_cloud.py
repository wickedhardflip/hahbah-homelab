from datetime import datetime, timezone

from app.collectors.eero import Eero
from app.merge import apply_eero, apply_eero_usage
from app.sidecar import build_jobs
from tests.test_merge import doc, fresh, kinds

NET = "/2.2/networks/1"
NOW = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)
HOURS = [{"time": f"t{i}", "value": 100} for i in range(3)]
FAKE = {"/account": {"networks": {"data": [{"url": NET}]}},
        NET: {"name": "home", "status": "connected"},
        NET + "/eeros": [{"location": "Hallway", "model": "eero 6", "status": "green", "gateway": True, "connected_clients_count": 4,
                          "os_version": "v7", "mac_address": "aa", "mesh_quality_bars": 5, "uptime": {"since_last_reboot_s": 90000},
                          "radio_channel_stats": {"band_5GHz_full": {"channel": 44, "channel_width": 80, "channel_utilization": 31, "client_count": 3}}},
                         {"location": "Office", "model": "eero 6", "status": "red"}],
        NET + "/devices": [
            {"connected": True, "wireless": False, "mac": "x", "url": "/d/1", "nickname": "NAS", "ip": "192.168.4.4", "first_seen": "2020-01-01T00:00:00Z"},
            {"connected": True, "wireless": True, "mac": "y", "url": "/d/2", "display_name": "Pixel", "first_seen": "2026-10-07T20:00:00Z",
             "connectivity": {"frequency": 5220, "signal": "-56 dBm", "score_bars": 4}, "usage": {"down_mbps": 2, "up_mbps": 1},
             "source": {"location": "Office"}, "profile": {"name": "Alex"}},
            {"connected": False, "wireless": True}],
        NET + "/data_usage": {"start": "a", "end": "b", "series": [{"type": "download", "values": HOURS}, {"type": "upload", "values": HOURS}]},
        NET + "/data_usage/devices": {"values": [{"url": "/d/1", "nickname": "NAS", "download": 1, "upload": 2}, {"url": "/d/2", "nickname": "Pixel", "download": 9, "upload": 8}]},
        NET + "/data_usage/eeros": {"values": [{"location": "Hallway", "download": 5, "upload": 6}]},
        NET + "/speedtest": [{"down_mbps": 900.5, "up_mbps": 800.1, "date": "d"}],
        NET + "/profiles": [{"name": "Unassigned", "paused": False, "devices": [1, 2], "default": True}],
        NET + "/updates": {"has_update": False, "target_firmware": "v8", "preferred_update_hour": 3},
        NET + "/guestnetwork": {"enabled": False, "name": "guest", "password": "SECRET"},
        NET + "/reservations": [{"ip": "192.168.4.5", "description": "Central", "mac": "z"}],
        NET + "/forwards": [{"description": "Plex", "gateway_port": 32400, "client_port": 32400, "protocol": "tcp", "ip": "192.168.4.5", "enabled": True}]}


def fake(path, token):
    return FAKE[path.split("?")[0]]


def live():
    return Eero("tok", fake).live(NOW)


def test_live_summary_drops_macs_and_flags_new():
    s = live()
    assert s["devices"] == {"connected": 2, "wired": 1, "known": 3, "new_24h": 1, "paused": 0}
    assert [c["name"] for c in s["clients"]] == ["NAS", "Pixel"] and s["clients"][1]["band"] == "5 GHz" and s["clients"][1]["new"]
    assert "mac" not in str(s) and "aa" not in str(s["nodes"])
    assert s["nodes"][0]["channels"]["band_5GHz_full"]["utilization"] == 31


def test_usage_summary_has_no_secrets_and_keys_devices_by_hash():
    u = Eero("tok", fake).daily_usage(NOW)
    ids = {c["id"]: c["name"] for c in live()["clients"]}
    assert u["usage_24h"]["down_bytes"] == 300 and {ids[k]: v for k, v in u["devices"].items()} == {"NAS": [1, 2], "Pixel": [9, 8]}
    assert "/d/" not in str(u) and "/d/" not in str(live())
    assert "SECRET" not in str(u) and "mac" not in str(u) and u["guest"] == {"enabled": False, "name": "guest"}


def test_history_has_windows_and_daily_totals():
    h = Eero("tok", fake).history(NOW)
    assert set(h) == {"d7", "d30", "daily"} and len(h["d7"]) == 2 and h["daily"] == [
        {"day": f"t{i}", "down": 100, "up": 100} for i in range(3)]


def test_network_url_cached_so_account_is_called_once():
    calls = []
    e = Eero("tok", lambda p, t: calls.append(p) or fake(p, t))
    e.live(NOW), e.live(NOW)
    assert calls.count("/account") == 1 and len(calls) == 7


def test_apply_eero_meta_alerts_and_snapshot_block():
    snap, nodes, alerts = fresh()
    apply_eero(doc(live(), interval=300), snap, nodes, alerts)
    assert nodes["router"]["meta"]["Devices"] == "2 connected (1 wired), 3 known"
    assert kinds(alerts) == [("eero_new_device", "info", "router")]   # node "red" in the fixture: no alert
    assert snap["eero"]["live"]["stale"] is False
    apply_eero_usage(doc(Eero("tok", fake).daily_usage(NOW), interval=3600), snap)
    assert len(snap["eero"]["usage"]["devices"]) == 2


def test_apply_eero_failed_doc_only_notes_why():
    snap, nodes, alerts = fresh()
    apply_eero(doc(None, ok=False), snap, nodes, alerts)
    apply_eero_usage(doc(None, ok=False), snap)
    assert alerts == [] and "unavailable" in nodes["router"]["meta"]["Devices"] and "eero" not in snap


def test_jobs_only_with_session():
    names = lambda env: [j.name for j in build_jobs(lambda k, d=None: env.get(k, d))]
    assert {"eero", "eero_usage", "eero_history"} <= set(names({"EERO_SESSION": "t"})) and "eero" not in names({})
