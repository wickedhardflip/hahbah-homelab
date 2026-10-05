"""The domain side of the edge: Porkbun (registration + DNS records, read-only calls) and DNS answers through the Eero.

Runs in the collector container only: it is the one place the Porkbun keys are available.
"""
import json
import random
import socket
import struct
import urllib.request

API = "https://api.porkbun.com/api/json/v3"


def porkbun(path: str, apikey: str, secret: str, timeout: float = 15.0) -> dict:
    body = json.dumps({"apikey": apikey, "secretapikey": secret}).encode()
    req = urllib.request.Request(API + path, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if data.get("status") != "SUCCESS":
        raise RuntimeError(f"Porkbun said: {data.get('message', 'error')}")
    return data


def _iso(porkbun_time: str) -> str:
    return porkbun_time.replace(" ", "T") + "Z"   # Porkbun answers in UTC without saying so


def domain_info(domains: dict, name: str) -> dict:
    d = next(x for x in domains["domains"] if x["domain"] == name)
    return {"name": name, "registrar": "Porkbun", "created": _iso(d["createDate"]), "expires": _iso(d["expireDate"]),
            "auto_renew": bool(int(d["autoRenew"])), "whois_privacy": bool(int(d["whoisPrivacy"])),
            "transfer_lock": bool(int(d["securityLock"]))}


def dns_drift(records: dict, expected: list) -> dict:
    a = {}
    for r in records["records"]:
        if r["type"] == "A":
            a.setdefault(r["name"], set()).add(r["content"])
    wrong = [e["name"] for e in expected if e["name"] in a and a[e["name"]] != {e["ip"]}]
    missing = [e["name"] for e in expected if e["name"] not in a]
    return {"wrong": wrong, "missing": missing}


def build_query(name: str, qid: int) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.rstrip(".").split("."))
    return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack(">HH", 1, 1)


def _skip_name(msg: bytes, i: int) -> int:
    while True:
        n = msg[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += n + 1


def parse_a_answers(msg: bytes, qid: int) -> list:
    if len(msg) < 12 or struct.unpack(">H", msg[:2])[0] != qid:
        return []
    qd, an = struct.unpack(">HH", msg[4:8])
    i = 12
    for _ in range(qd):
        i = _skip_name(msg, i) + 4
    ips = []
    for _ in range(an):
        i = _skip_name(msg, i)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", msg[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in msg[i:i + 4]))
        i += rdlen
    return ips


def resolve_via(server: str, name: str, timeout: float = 2.0) -> list:
    qid = random.randint(0, 0xFFFF)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(build_query(name, qid), (server, 53))
        return parse_a_answers(s.recv(1500), qid)


def check_edge(apikey: str, secret: str, domain: str, expected: list, eero_dns: str) -> dict:
    """Everything the collector writes to edge.json. Raises if Porkbun can't be read."""
    info = domain_info(porkbun("/domain/listAll", apikey, secret), domain)
    drift = dns_drift(porkbun(f"/dns/retrieve/{domain}", apikey, secret), expected)
    via = {}
    for e in expected:
        try:
            via[e["name"]] = resolve_via(eero_dns, e["name"])
        except Exception as ex:  # noqa: BLE001 (a truncated or odd reply must not lose the Porkbun data)
            via[e["name"]] = f"error: {type(ex).__name__}"
    bad = [n for n, ips in via.items() if not isinstance(ips, list) or next(x["ip"] for x in expected if x["name"] == n) not in ips]
    return {"domain": info, "dns": {"expected": expected, **drift, "via_eero_ok": not bad, "via_eero_bad": bad}}
