"""Portal side of the VPN monitor: merge.apply_vpn alert rules, the snapshot wiring, and the page code."""
import json
from datetime import datetime, timedelta, timezone

from app.merge import KNOCK_WARN, apply_vpn, read_collector
from app.snapshot import SnapshotBuilder
from tests.conftest import make_user, sign_in
from tests.test_merge import fresh, kinds
from tests.test_snapshot import APPS, DEPLOY_OK, EERO_OK, HOST_OK, TOPO

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def wgdoc(age=20, up=True, today=3, connected=False):
    return {"ok": True, "checked_at": "x", "age_s": age, "interface_up": up, "listen_port": 51820,
            "peers": [{"name": "phone", "connected": connected, "last_handshake": "2026-10-12T13:00:00Z", "rx": 1, "tx": 2, "endpoint_ip": "1.2.3.4"}],
            "sessions": [], "knocks": {"today": today, "daily": [], "top_sources": []}}


def ddnsdoc(ok=True, wan="203.0.113.9", dns="203.0.113.9", **kw):
    return {"checked_at": "x", "ok": ok, "wan_ip": wan, "dns_ip": dns, "updated_at": "2026-10-01T00:00:00Z", "error": None if ok else "api 500", **kw}


def run(wg=None, ddns=None, now=NOW, since=None):
    snap, nodes, alerts = fresh()
    apply_vpn(wg, ddns, snap, nodes, alerts, now, {} if since is None else since)
    return snap, nodes, alerts


def test_nothing_set_up_means_no_panel_and_no_alerts():
    snap, _, alerts = run(None, None)
    assert "vpn" not in snap and alerts == []


def test_healthy_tunnel_idle_peer_is_not_an_alert():
    snap, nodes, alerts = run(wgdoc(), ddnsdoc())
    assert alerts == [] and snap["vpn"]["wg"]["stale"] is False and snap["vpn"]["ddns"]["matches"] is True
    assert snap["vpn"]["ddns"]["name"] == "ticket.example.com"        # falls back when the updater doesn't say
    assert nodes["central"]["meta"]["VPN tunnel"] == "up · 0 of 1 peers connected"


def test_ddns_name_comes_from_the_file():
    snap, _, _ = run(None, ddnsdoc(name="other.example.com"))
    assert snap["vpn"]["ddns"]["name"] == "other.example.com"


def test_interface_down_and_stale_file_are_danger():
    assert kinds(run(wgdoc(up=False))[2]) == [("vpn_down", "crit", "ticket")]
    snap, nodes, alerts = run(wgdoc(age=301))
    assert kinds(alerts) == [("vpn_down", "crit", "ticket")] and snap["vpn"]["wg"]["stale"] is True
    assert run(wgdoc(age=299))[2] == []


def test_knocks_over_the_limit_is_caution():
    assert run(wgdoc(today=KNOCK_WARN))[2] == []
    assert kinds(run(wgdoc(today=KNOCK_WARN + 1))[2]) == [("vpn_knocks", "warn", "ticket")]


def test_ddns_failure_is_caution_at_once():
    assert kinds(run(None, ddnsdoc(ok=False))[2]) == [("ddns_drift", "warn", "ticket")]


def test_ddns_mismatch_waits_fifteen_minutes_and_clears():
    since: dict = {}
    d = ddnsdoc(dns="203.0.113.1")
    assert run(None, d, NOW, since)[2] == []
    assert run(None, d, NOW + timedelta(minutes=14), since)[2] == []
    assert kinds(run(None, d, NOW + timedelta(minutes=16), since)[2]) == [("ddns_drift", "warn", "ticket")]
    assert run(None, ddnsdoc(), NOW + timedelta(minutes=17), since)[2] == [] and since == {}


def test_never_alerts_because_peers_are_idle():
    doc = wgdoc(connected=False)
    doc["peers"] = [dict(doc["peers"][0], name=n) for n in ("a", "b", "c")]
    assert run(doc)[2] == []


def test_host_files_are_read_by_the_collector_reader(tmp_path):
    (tmp_path / "wireguard.json").write_text(json.dumps({"ok": True, "interval_s": 60, "checked_at": "2026-10-12T14:59:40Z", "interface_up": True}))
    (tmp_path / "ddns.json").write_text(json.dumps(ddnsdoc(checked_at="2026-10-12T14:59:00Z")))
    files = read_collector(tmp_path, NOW)
    assert files["wireguard"]["age_s"] == 20 and files["ddns"]["age_s"] == 60


def test_snapshot_builder_wires_the_files_and_keeps_the_mismatch_clock():
    files = {"wireguard": wgdoc(up=False), "ddns": ddnsdoc(dns="203.0.113.1")}
    clock = [NOW]
    b = SnapshotBuilder(TOPO, host=lambda: HOST_OK, eero=lambda: EERO_OK, deploy=lambda: DEPLOY_OK, apps=lambda: APPS,
                        collector=lambda: files, now=lambda: clock[0])
    snap = b.build()
    assert ("vpn_down", "crit", "ticket") in kinds(snap["alerts"]) and snap["vpn"]["wg"]["interface_up"] is False
    assert not any(a["kind"] == "ddns_drift" for a in snap["alerts"])
    clock[0] = NOW + timedelta(minutes=20)
    assert any(a["kind"] == "ddns_drift" for a in b.build()["alerts"])


def test_snapshot_has_no_vpn_key_without_files():
    b = SnapshotBuilder(TOPO, host=lambda: HOST_OK, eero=lambda: EERO_OK, deploy=lambda: DEPLOY_OK, apps=lambda: APPS, now=lambda: NOW)
    assert "vpn" not in b.build()


def test_page_has_the_vpn_section_and_alert_names(app, client):
    make_user(app)
    sign_in(client)
    page = client.get("/").text
    assert "vpn_down" in page and "ddns_drift" in page and "vpn_knocks" in page
    js = client.get("/static/noc.js").text
    assert 'section("VPN"' in js and "innerHTML" not in js and "vpn.example.com" not in js


def test_vpn_data_reaches_the_api(app, client, builder):
    builder.snapshot = {**builder.snapshot, "vpn": {"wg": {"interface_up": True}}}
    make_user(app)
    sign_in(client)
    assert client.get("/api/snapshot").json()["vpn"]["wg"]["interface_up"] is True
