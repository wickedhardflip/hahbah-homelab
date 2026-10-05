"""The certificate Caddy is serving, read over TLS from inside the stack. Also tells us Caddy is up."""
import socket
import ssl

from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID


def fetch_der(host: str, port: int, sni: str, timeout: float = 3.0) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # only reading the dates; Caddy's own renewal proves the chain
    with socket.create_connection((host, port), timeout=timeout) as raw, ctx.wrap_socket(raw, server_hostname=sni) as tls:
        return tls.getpeercert(binary_form=True)


def _z(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_cert(der: bytes) -> dict:
    c = x509.load_der_x509_certificate(der)
    try:
        names = c.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = [a.value for a in c.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    org = [a.value for a in c.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)]
    cn = [a.value for a in c.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)]
    issuer = f"{org[0]} ({cn[0]})" if org and cn else (org or cn or ["unknown"])[0]
    return {"names": list(names), "issuer": issuer, "issued": _z(c.not_valid_before_utc), "expires": _z(c.not_valid_after_utc)}
