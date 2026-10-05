import json
import struct
from pathlib import Path

from app.collectors.edge import build_query, dns_drift, domain_info, parse_a_answers

FX = Path(__file__).parent / "fixtures/collector"
DOMAINS = json.loads((FX / "porkbun_domains.json").read_text(encoding="utf-8"))
RECORDS = json.loads((FX / "porkbun_dns.json").read_text(encoding="utf-8"))
EXPECTED = [{"name": n, "ip": "192.168.4.5", "host": "central"}
            for n in ("home.hahbah.com", "jobs.hahbah.com", "social.hahbah.com", "legacymonitor.hahbah.com",
                      "replexon.hahbah.com", "plex.hahbah.com", "tautulli.hahbah.com")]


def test_domain_info_from_porkbun():
    d = domain_info(DOMAINS, "hahbah.com")
    assert d["name"] == "hahbah.com" and d["registrar"] == "Porkbun"
    assert d["expires"].startswith("2027-09-30T") and d["expires"].endswith("Z")
    assert d["auto_renew"] is True and d["whois_privacy"] is True and d["transfer_lock"] is True


def test_no_drift_when_records_match():
    assert dns_drift(RECORDS, EXPECTED) == {"wrong": [], "missing": []}


def test_drift_finds_wrong_and_missing():
    exp = EXPECTED + [{"name": "new.hahbah.com", "ip": "192.168.4.5", "host": "central"}]
    exp[1] = {"name": "jobs.hahbah.com", "ip": "192.168.4.99", "host": "apps"}
    assert dns_drift(RECORDS, exp) == {"wrong": ["jobs.hahbah.com"], "missing": ["new.hahbah.com"]}


def answer(query: bytes, ips: list) -> bytes:
    """A minimal DNS response to `query` carrying A records for `ips` (name compression pointer to the question)."""
    qid = query[:2]
    header = qid + struct.pack(">HHHHH", 0x8180, 1, len(ips), 0, 0)
    question = query[12:]
    rrs = b"".join(b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 300, 4) + bytes(int(x) for x in ip.split(".")) for ip in ips)
    return header + question + rrs


def test_query_and_answer_round_trip():
    q = build_query("home.hahbah.com", 0x1234)
    assert q[:2] == b"\x12\x34" and b"\x04home\x06hahbah\x03com\x00" in q
    assert parse_a_answers(answer(q, ["192.168.4.5"]), 0x1234) == ["192.168.4.5"]


def test_answer_with_wrong_id_is_ignored():
    q = build_query("home.hahbah.com", 0x1234)
    assert parse_a_answers(answer(q, ["192.168.4.5"]), 0x9999) == []


def test_one_odd_eero_reply_does_not_lose_the_porkbun_data(monkeypatch):
    import app.collectors.edge as edge
    monkeypatch.setattr(edge, "porkbun", lambda path, k, s: DOMAINS if "listAll" in path else RECORDS)
    def bad(server, name):
        raise struct.error("unpack requires a buffer of 10 bytes")
    monkeypatch.setattr(edge, "resolve_via", bad)
    r = edge.check_edge("k", "s", "hahbah.com", EXPECTED, "192.168.4.1")
    assert r["domain"]["auto_renew"] is True and r["dns"]["via_eero_ok"] is False
