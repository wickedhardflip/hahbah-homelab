"""Ask the Eero over local UPnP whether the internet is up. No login; read-only SOAP calls."""
from http.client import HTTPException
import re
import urllib.error
import urllib.request

WAN_IP = "urn:schemas-upnp-org:service:WANIPConnection:1"


def default_http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 2.0) -> str:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def control_urls(description: str) -> dict:
    out = {}
    for block in re.findall(r"<service>(.*?)</service>", description, re.S):
        st, ctl = re.search(r"<serviceType>([^<]+)<", block), re.search(r"<controlURL>([^<]+)<", block)
        if st and ctl:
            out[st.group(1)] = ctl.group(1)
    return out


def soap_fields(xml: str) -> dict:
    return dict(re.findall(r"<(New\w+)>([^<]*)</", xml))


def _call(http, url: str, action: str) -> dict:
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{WAN_IP}"/></s:Body></s:Envelope>')
    headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{WAN_IP}#{action}"'}
    return soap_fields(http(url, body.encode(), headers, 2.0))


def eero_status(igd_url: str, http=default_http) -> dict:
    try:
        ctl = control_urls(http(igd_url, None, None, 2.0)).get(WAN_IP)
        if not ctl:
            return {"reachable": True, "connected": None, "uptime_s": 0, "last_error": "no WANIPConnection service", "public_ip": None}
        if not ctl.startswith("http"):
            ctl = "/".join(igd_url.split("/")[:3]) + ctl
        status = _call(http, ctl, "GetStatusInfo")
        ip = _call(http, ctl, "GetExternalIPAddress").get("NewExternalIPAddress")
        return {"reachable": True, "connected": status.get("NewConnectionStatus") == "Connected",
                "uptime_s": int(status.get("NewUptime") or 0), "last_error": status.get("NewLastConnectionError"),
                "public_ip": ip or None}
    except (urllib.error.URLError, HTTPException, OSError, ValueError) as e:
        return {"reachable": False, "error": type(e).__name__}
