from app.dnssync import desired, plan

SITE = {"domain": "hahbah.com", "edge_host": "central", "hosts": {"central": {"ip": "192.168.4.5"}},
        "apps": [{"id": "jobs", "subdomain": "jobs"}, {"id": "plex", "subdomain": "plex"}]}


def rec(name, type_, content):
    return {"name": name, "type": type_, "content": content, "id": "1"}


def test_desired_includes_home_and_every_app():
    assert desired(SITE) == {"home.hahbah.com": "192.168.4.5", "jobs.hahbah.com": "192.168.4.5", "plex.hahbah.com": "192.168.4.5"}


def test_plan_creates_missing_updates_wrong_and_leaves_correct():
    records = [rec("home.hahbah.com", "A", "192.168.4.5"), rec("jobs.hahbah.com", "A", "192.168.4.9"),
               rec("hahbah.com", "MX", "fwd1.porkbun.com"), rec("*.hahbah.com", "CNAME", "uixie.porkbun.com")]
    acts = {a["name"]: a["action"] for a in plan(desired(SITE), records)}
    assert acts == {"home.hahbah.com": "ok", "jobs.hahbah.com": "update", "plex.hahbah.com": "create"}


def test_second_run_is_a_no_op():
    records = [rec(n, "A", ip) for n, ip in desired(SITE).items()]
    assert {a["action"] for a in plan(desired(SITE), records)} == {"ok"}


def test_existing_cname_on_a_name_is_a_conflict_not_touched():
    acts = plan({"jobs.hahbah.com": "192.168.4.5"}, [rec("jobs.hahbah.com", "CNAME", "elsewhere.example")])
    assert acts == [{"action": "conflict", "name": "jobs.hahbah.com", "ip": "192.168.4.5", "current": "CNAME elsewhere.example"}]


def test_txt_or_mx_beside_a_name_is_not_a_conflict():
    acts = plan({"jobs.hahbah.com": "192.168.4.5"}, [rec("jobs.hahbah.com", "TXT", "v=spf1"), rec("jobs.hahbah.com", "A", "192.168.4.5")])
    assert acts[0]["action"] == "ok"
    acts = plan({"jobs.hahbah.com": "192.168.4.5"}, [rec("jobs.hahbah.com", "TXT", "hello")])
    assert acts[0]["action"] == "create"


def test_every_a_record_is_compared():
    acts = plan({"jobs.hahbah.com": "192.168.4.5"}, [rec("jobs.hahbah.com", "A", "192.168.4.5"), rec("jobs.hahbah.com", "A", "10.0.0.9")])
    assert acts[0]["action"] == "update" and acts[0]["current"] == "10.0.0.9, 192.168.4.5"


def test_non_json_api_errors_become_an_error_status(monkeypatch):
    import io
    import urllib.error
    from app import dnssync

    def boom(body, code):
        def opener(req, timeout=0):
            raise urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))
        return opener
    for body in (b"<html>502 Bad Gateway</html>", b"", b"[1]"):
        monkeypatch.setattr(dnssync.urllib.request, "urlopen", boom(body, 502))
        r = dnssync._call("/x", {})
        assert r["status"] == "ERROR" and "502" in r["message"]
    monkeypatch.setattr(dnssync.urllib.request, "urlopen", boom(b'{"status":"ERROR","message":"bad key"}', 400))
    assert dnssync._call("/x", {})["message"] == "bad key"

    def down(req, timeout=0):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(dnssync.urllib.request, "urlopen", down)
    assert dnssync._call("/x", {})["status"] == "ERROR"
