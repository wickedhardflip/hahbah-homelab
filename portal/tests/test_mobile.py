"""Phone shell, manifest, service worker, view choice and web push. Fake sender: no network, no real keys."""
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import push
from app.main import create_app
from conftest import FakeBuilder, make_user, sign_in

STATIC = Path(__file__).parent.parent / "app" / "static"
OK = {"origin": "https://home.example.com"}
IPHONE = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"}
IPAD = {"user-agent": "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15"}
SUB = {"endpoint": "https://push.example.test/abc", "keys": {"p256dh": "BPkey", "auth": "authkey"}}


def client_for(settings, **over):
    app = create_app(replace(settings, **over), builder=FakeBuilder(), collect=False)
    make_user(app)
    c = TestClient(app)
    sign_in(c)
    return app, c


@pytest.fixture
def client(settings):
    return client_for(settings)[1]


@pytest.fixture
def pushed(settings):
    return client_for(settings, vapid_private="priv", vapid_public="pubkey", vapid_subject="mailto:a@b.test")


def test_manifest_fields(client):
    anon = TestClient(client.app)
    r = anon.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["start_url"] == "/m" and m["display"] == "standalone" and m["scope"] == "/"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes and any(i["purpose"] == "maskable" for i in m["icons"])
    for i in m["icons"]:
        assert (STATIC / i["src"].removeprefix("/static/")).is_file()


def test_service_worker_scope_header(client):
    r = TestClient(client.app).get("/sw.js")
    assert r.status_code == 200 and r.headers["service-worker-allowed"] == "/" and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"
    # every file the worker precaches exists
    for path in ("/static/mobile.css", "/static/mobile.js", "/static/plugs.js", "/static/site/favicon.svg", "/static/site/icon-192.png"):
        assert path in r.text and (STATIC / path.removeprefix("/static/")).is_file()


def test_shell_needs_sign_in_and_has_tabs(client):
    anon = TestClient(client.app)
    assert anon.get("/m", follow_redirects=False).headers["location"] == "/login"
    r = client.get("/m")
    assert r.status_code == 200
    for needle in ('rel="manifest"', "viewport-fit=cover", "apple-mobile-web-app-capable", "/static/mobile.js", 'id="tabbar"'):
        assert needle in r.text
    js = (STATIC / "mobile.js").read_text(encoding="utf-8")
    for tab in ("Home", "Plugs", "Alerts", "Ask", "More"):
        assert f'"{tab}"' in js
    assert "/ask" in js and "innerHTML" not in js


def test_phone_redirect_and_view_choice(client):
    assert client.get("/", headers=IPHONE, follow_redirects=False).headers["location"] == "/m"
    assert client.get("/", headers=IPAD, follow_redirects=False).status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200   # desktop untouched
    r = client.get("/view/desktop", follow_redirects=False)
    assert r.headers["location"] == "/" and "hl_view=desktop" in r.headers["set-cookie"]
    assert client.get("/", headers=IPHONE, follow_redirects=False).status_code == 200   # cookie wins over the UA hint
    client.get("/view/mobile", follow_redirects=False)
    assert client.get("/", headers=IPHONE, follow_redirects=False).headers["location"] == "/m"
    assert client.get("/view/nope", follow_redirects=False).status_code == 404


def test_user_menu_offers_mobile_view(client):
    assert "/view/mobile" in client.get("/about").text


def test_push_off_without_keys(client):
    assert client.get("/api/push/config").json() == {"enabled": False, "key": ""}
    assert client.post("/api/push/subscribe", json=SUB, headers=OK).status_code == 404
    assert push.send_all(client.app.state.settings, client.app.state.settings.data_dir / "x.json", sender=lambda *a: 1 / 0) == 0


def test_push_config_exposes_only_the_public_key(pushed):
    app, c = pushed
    assert c.get("/api/push/config").json() == {"enabled": True, "key": "pubkey"}
    assert "priv" not in repr(app.state.settings)


def test_subscribe_validates_and_stores(pushed):
    app, c = pushed
    f = app.state.settings.data_dir / "push_subs.json"
    assert c.post("/api/push/subscribe", json=SUB, headers={"origin": "https://evil.test"}).status_code == 403
    assert c.post("/api/push/subscribe", json={"endpoint": "http://x", "keys": SUB["keys"]}, headers=OK).status_code == 422
    assert c.post("/api/push/subscribe", json={"endpoint": SUB["endpoint"]}, headers=OK).status_code == 422
    assert TestClient(app).post("/api/push/subscribe", json=SUB, headers=OK).status_code == 401
    assert c.post("/api/push/subscribe", json=SUB, headers=OK).status_code == 201
    assert json.loads(f.read_text())[SUB["endpoint"]]["user"] == "alex"
    assert c.post("/api/push/unsubscribe", json=SUB, headers=OK).json() == {"subscribed": False}
    assert json.loads(f.read_text()) == {}


def test_subscription_cap(tmp_path):
    f = tmp_path / "s.json"
    for i in range(push.MAX_SUBS):
        assert push.add(f, {**SUB, "endpoint": f"https://p.test/{i}"}, "u")
    assert not push.add(f, {**SUB, "endpoint": "https://p.test/extra"}, "u")
    assert push.add(f, {**SUB, "endpoint": "https://p.test/3"}, "u")   # re-registering a known device is fine


def test_send_all_prunes_dead_and_is_generic(settings, tmp_path):
    f = tmp_path / "s.json"
    cfg = replace(settings, vapid_private="p", vapid_public="k", vapid_subject="mailto:a@b.test")
    push.add(f, {**SUB, "endpoint": "https://p.test/ok"}, "u")
    push.add(f, {**SUB, "endpoint": "https://p.test/gone"}, "u")
    seen = []

    class Gone(Exception):
        response = type("R", (), {"status_code": 410})()

    def sender(sub, data, s):
        if sub["endpoint"].endswith("gone"):
            raise Gone()
        seen.append(json.loads(data))

    assert push.send_all(cfg, f, sender=sender) == 1
    assert list(push.read(f)) == ["https://p.test/ok"]
    assert seen[0]["url"] == "/m#alerts" and "Danger" in seen[0]["body"]


def test_test_push_is_admin_only(pushed, monkeypatch):
    app, c = pushed
    calls = []
    monkeypatch.setattr(push, "_webpush", lambda sub, data, s: calls.append(sub))
    c.post("/api/push/subscribe", json=SUB, headers=OK)
    assert c.post("/api/push/test", headers=OK).status_code == 403   # a plain user
    with app.state.SessionLocal() as db:
        from app.models import User
        db.query(User).one().is_admin = True
        db.commit()
    assert c.post("/api/push/test", headers=OK).json() == {"sent": 1}
    assert c.post("/api/push/test", headers={"origin": "https://evil.test"}).status_code == 403


def test_new_danger_triggers_push_once(settings):
    app = create_app(replace(settings, vapid_private="p", vapid_public="k", vapid_subject="mailto:a@b.test"), builder=FakeBuilder(), collect=False)
    got = []
    app.state.recorder.on_danger = got.append
    snap = {"nodes": [], "links": [], "alerts": [{"id": "x", "severity": "crit", "kind": "app_down", "target": "plex", "message": "down", "meta": {}}]}
    for _ in range(3):
        app.state.recorder.observe(snap)
    assert len(got) == 1 and got[0][0]["key"] == "app_down:plex"


def test_sw_cache_is_cleared_on_sign_out_hook():
    js = (STATIC / "mobile.js").read_text(encoding="utf-8")
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'postMessage("clear")' in js and '"clear"' in sw
