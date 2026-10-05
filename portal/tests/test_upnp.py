import urllib.error

from app.collectors.upnp import eero_status

IGD = "http://192.168.4.1:1900/igd.xml"
DESC = """<root><device><serviceList>
<service><serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType><controlURL>/ctl/CommonIfCfg</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType><controlURL>/ctl/IPConn</controlURL></service>
</serviceList></device></root>"""
STATUS = "<s:Envelope><s:Body><u:GetStatusInfoResponse><NewConnectionStatus>Connected</NewConnectionStatus><NewLastConnectionError>ERROR_NONE</NewLastConnectionError><NewUptime>988131</NewUptime></u:GetStatusInfoResponse></s:Body></s:Envelope>"
IP = "<s:Envelope><s:Body><u:GetExternalIPAddressResponse><NewExternalIPAddress>203.0.113.7</NewExternalIPAddress></u:GetExternalIPAddressResponse></s:Body></s:Envelope>"


def fake_http(calls):
    def http(url, data=None, headers=None, timeout=2.0):
        calls.append((url, (headers or {}).get("SOAPAction")))
        if url == IGD:
            return DESC
        return STATUS if "GetStatusInfo" in headers["SOAPAction"] else IP
    return http


def test_connected_eero_reports_uptime_and_ip():
    calls = []
    s = eero_status(IGD, http=fake_http(calls))
    assert s == {"reachable": True, "connected": True, "uptime_s": 988131, "last_error": "ERROR_NONE", "public_ip": "203.0.113.7"}
    assert calls[1][0] == "http://192.168.4.1:1900/ctl/IPConn"


def test_disconnected_is_reported():
    s = eero_status(IGD, http=lambda u, data=None, headers=None, timeout=2.0: DESC if u == IGD else STATUS.replace("Connected", "Disconnected"))
    assert s["reachable"] is True and s["connected"] is False


def test_unreachable_eero_returns_error_not_exception():
    def boom(url, data=None, headers=None, timeout=2.0):
        raise urllib.error.URLError("timed out")
    assert eero_status(IGD, http=boom) == {"reachable": False, "error": "URLError"}


def test_missing_wan_service_is_handled():
    s = eero_status(IGD, http=lambda u, data=None, headers=None, timeout=2.0: "<root></root>")
    assert s["reachable"] is True and s["connected"] is None


def test_garbled_http_reply_is_unreachable_not_exception():
    import http.client
    def garbled(url, data=None, headers=None, timeout=2.0):
        raise http.client.BadStatusLine("garbage")
    assert eero_status(IGD, http=garbled) == {"reachable": False, "error": "BadStatusLine"}
