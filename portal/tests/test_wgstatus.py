"""hosts/central/wireguard/wgstatus.py: the dump parser, sessions, knock counting, pruning and the written file."""
import importlib.util
import json
from datetime import date, datetime, timezone
from pathlib import Path

SRC = Path(__file__).parents[2] / "hosts/central/wireguard/wgstatus.py"
spec = importlib.util.spec_from_file_location("wgstatus", SRC)
wg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wg)

PRIV, PSK = "PRIVATEKEYPRIVATEKEYPRIVATEKEYPRIVATEKEY0=", "PRESHAREDPRESHAREDPRESHAREDPRESHARED0="
PUB_A, PUB_B, PUB_C = "AAAAAAAApubkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "BBBBBBBBpubkeyBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=", "CCCCCCCCpubkeyCCCCCCCCCCCCCCCCCCCCCCCCCCCC="
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc).timestamp()
DUMP = "\n".join(["\t".join([PRIV, "SERVERPUB=", "51820", "off"]),
                  "\t".join([PUB_A, PSK, "198.51.100.23:40000", "10.8.0.2/32", str(int(NOW) - 30), "1000", "2000", "25"]),
                  "\t".join([PUB_B, "(none)", "[2001:db8::7]:5555", "10.8.0.3/32", str(int(NOW) - 7200), "10", "20", "off"]),
                  "\t".join([PUB_C, "(none)", "(none)", "10.8.0.4/32", "0", "0", "0", "off"])])
CONF = f"""[Interface]
PrivateKey = {PRIV}
ListenPort = 51820

# peer: phone
[Peer]
PublicKey = {PUB_A}
PresharedKey = {PSK}
AllowedIPs = 10.8.0.2/32

# peer: laptop
[Peer]
PublicKey = {PUB_B}
AllowedIPs = 10.8.0.3/32

[Peer]
PublicKey = {PUB_C}
AllowedIPs = 10.8.0.4/32
"""


def peer(hs, rx=0, tx=0, ip="1.2.3.4"):
    return {"pub": PUB_A, "endpoint_ip": ip, "handshake": hs, "rx": rx, "tx": tx}


def test_parse_dump_keeps_no_keys():
    port, peers = wg.parse_dump(DUMP)
    assert port == 51820 and len(peers) == 3
    assert peers[0] == {"pub": PUB_A, "endpoint_ip": "198.51.100.23", "handshake": int(NOW) - 30, "rx": 1000, "tx": 2000}
    assert peers[1]["endpoint_ip"] == "2001:db8::7" and peers[2]["endpoint_ip"] is None
    assert PRIV not in json.dumps(peers) and PSK not in json.dumps(peers)


def test_parse_dump_empty():
    assert wg.parse_dump("") == (None, [])


def test_names_from_peer_comments():
    names = wg.parse_names(CONF)
    assert names == {PUB_A: "phone", PUB_B: "laptop"}   # the un-commented peer has no name
    assert PRIV not in json.dumps(names)


def test_knock_regex_matches_the_kernel_lines():
    log = "\n".join([
        "wireguard: wg0: Invalid MAC of handshake, dropping packet from 192.0.2.14:41234",
        "wireguard: wg0: Invalid handshake initiation from 192.0.2.14:41235",
        "wireguard: wg0: Invalid handshake response from [2001:db8::99]:1000",
        "wireguard: wg0: Handshake for peer 3 (1.2.3.4:51820) did not complete after 5 seconds, retrying",
        "wireguard: wg0: Receiving handshake initiation from peer 1 (1.2.3.4:5)",
        "usb 1-1: new device from 1.2.3.4:5, Invalid thing",
        "wireguard: wg0: Invalid MAC of handshake, dropping packet from 198.51.100.2:9"])
    assert wg.count_knocks(log) == {"192.0.2.14": 2, "2001:db8::99": 1, "198.51.100.2": 1}


def test_endpoint_ip_drops_port():
    assert wg.endpoint_ip("1.2.3.4:51820") == "1.2.3.4" and wg.endpoint_ip("[::1]:5") == "::1" and wg.endpoint_ip("(none)") is None


def test_session_opens_continues_and_reopens_after_a_gap():
    h = {}
    wg.update_sessions(h, "phone", peer(1000, 100, 50), 1010)
    s = h["sessions"]
    assert len(s) == 1 and s[0]["start"] == wg.iso(1000) and s[0]["rx_bytes"] == 100
    wg.update_sessions(h, "phone", peer(1120, 400, 80), 1130)      # re-key 120 s later: same session
    assert len(s) == 1 and s[0]["rx_bytes"] == 400 and s[0]["tx_bytes"] == 80 and s[0]["last_seen"] == wg.iso(1130)
    wg.update_sessions(h, "phone", peer(1120, 450, 90), 1500)      # idle, no new handshake: bytes add, last_seen stays put
    assert len(s) == 1 and s[0]["last_seen"] == wg.iso(1130) and s[0]["rx_bytes"] == 450
    wg.update_sessions(h, "phone", peer(2000, 500, 95), 2010)      # new handshake after a long gap: new session
    assert len(s) == 2 and s[1]["start"] == wg.iso(2000) and s[1]["rx_bytes"] == 50


def test_counter_reset_and_never_connected_peer():
    h = {}
    wg.update_sessions(h, "phone", peer(1000, 900, 900), 1010)
    wg.update_sessions(h, "phone", peer(1050, 30, 20), 1060)       # interface restarted: counters began again
    assert h["sessions"][0]["rx_bytes"] == 930
    wg.update_sessions(h, "ghost", peer(0), 1060)
    assert [x["peer"] for x in h["sessions"]] == ["phone"]


def test_prune_drops_old_sessions_and_knock_days():
    today = date(2026, 10, 12)
    h = {"sessions": [{"peer": "a", "start": wg.iso(NOW - 100 * 86400), "last_seen": wg.iso(NOW - 100 * 86400)},
                      {"peer": "a", "start": wg.iso(NOW - 86400), "last_seen": wg.iso(NOW - 86400)}],
         "knocks": {"daily": {"2026-10-12": 2, "2026-08-01": 9},
                    "sources": {"1.1.1.1": {"days": {"2026-10-12": 2}, "last_seen": "x"}, "2.2.2.2": {"days": {"2026-08-01": 9}, "last_seen": "y"}}}}
    wg.prune(h, NOW, today)
    assert len(h["sessions"]) == 1 and h["knocks"]["daily"] == {"2026-10-12": 2} and list(h["knocks"]["sources"]) == ["1.1.1.1"]


def test_knock_summary_has_30_days_and_top_ten():
    today = date(2026, 10, 12)
    h = {}
    wg.update_knocks(h, {f"10.0.0.{i}": i for i in range(1, 15)}, NOW, today.isoformat())
    k = wg.knocks_summary(h, today)
    assert len(k["daily"]) == 30 and k["daily"][-1] == {"date": "2026-10-12", "count": sum(range(1, 15))} and k["today"] == 105
    assert len(k["top_sources"]) == 10 and k["top_sources"][0]["ip"] == "10.0.0.14"


def test_main_writes_status_and_history_without_keys(tmp_path, monkeypatch):
    conf = tmp_path / "wg0.conf"
    conf.write_text(CONF)
    monkeypatch.setattr(wg, "CONF", str(conf))
    monkeypatch.setattr(wg, "HISTORY", str(tmp_path / "w/history.json"))
    monkeypatch.setattr(wg, "STATUS", str(tmp_path / "c/wireguard.json"))
    monkeypatch.setattr(wg, "run", lambda cmd, t: DUMP if cmd[0] == "wg" else "wireguard: wg0: Invalid MAC of handshake, dropping packet from 192.0.2.14:1\n")
    assert wg.main(NOW) == 0
    raw = (tmp_path / "c/wireguard.json").read_text()
    out = json.loads(raw)
    hist = (tmp_path / "w/history.json").read_text()
    assert all(k not in raw + hist for k in (PRIV, PSK, PUB_A, PUB_B))
    assert out["interface_up"] is True and out["listen_port"] == 51820
    assert [(p["name"], p["connected"]) for p in out["peers"]] == [("phone", True), ("laptop", False), ("CCCCCCCC", False)]
    assert out["peers"][1]["last_handshake"] == wg.iso(int(NOW) - 7200) and out["knocks"]["top_sources"][0]["ip"] == "192.0.2.14"
    assert len(out["sessions"]) == 2 and not list(tmp_path.rglob("*.tmp"))


def test_wg_failure_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(wg, "HISTORY", str(tmp_path / "history.json"))
    monkeypatch.setattr(wg, "STATUS", str(tmp_path / "wireguard.json"))
    monkeypatch.setattr(wg, "run", lambda cmd, t: None)
    assert wg.main(NOW) == 0
    out = json.loads((tmp_path / "wireguard.json").read_text())
    assert out["interface_up"] is False and out["peers"] == [] and out["knocks"]["today"] == 0
