import urllib.error
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.collectors.cert import parse_cert
from app.collectors.health import check_all, health_targets


def der_cert(days_left=88):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.hahbah.com")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Let's Encrypt"),
                                    x509.NameAttribute(NameOID.COMMON_NAME, "YE1")]))
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days_left))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("*.hahbah.com"), x509.DNSName("hahbah.com")]), False)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER)


def test_parse_cert():
    c = parse_cert(der_cert())
    assert c["names"] == ["*.hahbah.com", "hahbah.com"] and c["issuer"] == "Let's Encrypt (YE1)"
    assert c["issued"] == "2026-09-30T00:00:00Z" and c["expires"] == "2026-12-28T00:00:00Z"


APPS = [{"id": "jobs", "port": 8080, "health": "/login", "host": "central"},
        {"id": "tautulli", "port": 8181, "health": "/status", "host": "central", "upstream": "tautulli:8181"},
        {"id": "nohealth", "port": 1, "host": "central"}]


def test_targets_use_upstream_or_the_host_gateway():
    t = dict(health_targets(APPS, "host.docker.internal", "http://192.168.4.9:11434"))
    assert t == {"jobs": "http://host.docker.internal:8080/login", "tautulli": "http://tautulli:8181/status",
                 "ollama": "http://192.168.4.9:11434/api/tags"}


def test_check_all_maps_answers_and_failures():
    def get(url, timeout):
        if "8080" in url:
            return 200, b""
        if "8181" in url:
            raise urllib.error.HTTPError(url, 503, "down", {}, None)
        if "11434" in url:
            return 200, b'{"models": [{"name": "qwen2.5:3b"}, {"name": "llava:7b"}]}'
        raise OSError("refused")
    r = check_all([("jobs", "http://h:8080/login"), ("tautulli", "http://t:8181/status"),
                   ("ollama", "http://pc:11434/api/tags"), ("gone", "http://h:1/")], get=get)
    assert r["jobs"]["ok"] is True and r["jobs"]["code"] == 200 and r["jobs"]["ms"] >= 0
    assert r["tautulli"] == {"ok": False, "code": 503, "ms": r["tautulli"]["ms"], "error": "HTTP 503"}
    assert r["ollama"]["models"] == ["qwen2.5:3b", "llava:7b"]
    assert r["gone"]["ok"] is False and r["gone"]["error"] == "OSError"


def test_redirects_and_auth_walls_count_as_up():
    r = check_all([("a", "u1"), ("b", "u2")], get=lambda url, timeout: (302, b"") if url == "u1" else (401, b""))
    assert r["a"]["ok"] and r["b"]["ok"]


def test_not_found_is_down_but_auth_walls_are_up():
    def get(url, timeout):
        raise urllib.error.HTTPError(url, 404 if "a" in url else 401, "x", {}, None)
    r = check_all([("a", "http://a/"), ("b", "http://b/")], get=lambda url, timeout: (404, b"") if url.endswith("a/") else (401, b""))
    assert r["a"]["ok"] is False and r["a"]["error"] == "HTTP 404" and r["b"]["ok"] is True
