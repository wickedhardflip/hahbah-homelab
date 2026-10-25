from conftest import FakeBuilder, make_user, sign_in


def test_api_requires_sign_in(client):
    for path in ("/api/snapshot", "/api/apps"):
        r = client.get(path)
        assert r.status_code == 401 and r.json() == {"detail": "Sign in required."}


def test_snapshot_after_sign_in(app, client, builder):
    make_user(app)
    sign_in(client)
    r = client.get("/api/snapshot")
    assert r.status_code == 200 and r.json()["schema"] == "homelab.snapshot/v1"


def test_snapshot_etag_answers_304_until_it_changes(app, client, builder):
    make_user(app)
    sign_in(client)
    first = client.get("/api/snapshot")
    tag = first.headers["etag"]
    assert first.headers["cache-control"] == "private, no-cache"
    again = client.get("/api/snapshot", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == tag
    builder.snapshot = {**builder.snapshot, "alerts": [{"id": "x", "kind": "app", "target": "t", "state": "caution", "text": "t"}]}
    app.state.store.refresh()
    changed = client.get("/api/snapshot", headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag


def test_3d_view_is_a_lazy_static_module_not_inline(app, client):
    make_user(app)
    sign_in(client)
    page = client.get("/").text
    assert "static/map3d.js" in page and "import * as THREE" not in page   # fetched only when 3D opens
    js = client.get("/static/map3d.js")
    assert js.status_code == 200 and 'import * as THREE from "three"' in js.text and "H.views[\"3d\"]" in js.text


def test_apps_lists_the_registry(app, client, settings):
    settings.apps_file.write_text("apps:\n  - {id: jobs, name: Example App, subdomain: jobs, host: central, "
                                  "port: 8080, health: /login, lan_url: 'http://192.168.1.5:8080'}\n")
    make_user(app)
    sign_in(client)
    assert [a["id"] for a in client.get("/api/apps").json()["apps"]] == ["jobs"]


def test_dashboard_embeds_the_snapshot_and_user(app, client):
    make_user(app)
    sign_in(client)
    r = client.get("/")
    assert r.status_code == 200
    assert "window.HOMELAB_SNAPSHOT" in r.text and "alex" in r.text
    assert 'action="/logout"' in r.text


def test_snapshot_text_cannot_break_out_of_the_script_tag(settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    evil = FakeBuilder({"schema": "homelab.snapshot/v1", "nodes": [], "links": [], "alerts": [],
                        "note": "</script><script>alert(1)</script>"})
    app = create_app(settings, builder=evil, collect=False)
    with TestClient(app) as c:
        make_user(app)
        sign_in(c)
        assert "</script><script>alert(1)" not in c.get("/").text


def test_broken_apps_file_is_reported_not_500(app, client, settings):
    settings.apps_file.write_text("apps:\n  - {id: jobs, name: [unclosed\n")
    make_user(app)
    sign_in(client)
    r = client.get("/api/apps")
    assert r.status_code == 200 and r.json()["apps"] == [] and "valid YAML" in r.json()["error"]


def test_dashboard_has_the_faded_station_background(app, client):
    make_user(app)
    sign_in(client)
    page = client.get("/").text
    assert "/static/site/station-1920.jpg" in page and "--bg-veil" in page
    img = client.get("/static/site/station-1920.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"


def test_dashboard_has_the_plex_card_code(app, client):
    make_user(app)
    sign_in(client)
    body = client.get("/").text
    assert "function plexCard(" in body and "/api/plex/user/" in body and ".detail.wide" in body


def test_live_refresh_carries_the_plex_stats(app, client):
    make_user(app)
    sign_in(client)
    body = client.get("/").text
    refresh = body.split("async function refresh()")[1].split("setInterval(refresh")[0]
    assert "S.plex = fresh.plex" in refresh      # otherwise the card and badge freeze at page-load values


def test_every_page_has_the_homelab_favicon(app, client):
    links = ('rel="icon" href="/static/site/favicon.svg"', 'rel="apple-touch-icon" href="/static/site/apple-touch-icon.png"')
    assert all(l in client.get("/login").text for l in links)
    make_user(app)
    sign_in(client)
    assert all(l in client.get("/").text for l in links)
    assert all(l in client.get("/logout").text for l in links)              # sign-out confirmation page
    svg = client.get("/static/site/favicon.svg")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")
    png = client.get("/static/site/apple-touch-icon.png")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_static_files_get_cache_headers(client):
    r = client.get("/static/site/favicon.svg")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=86400"
    assert client.get("/static/noc.js").headers["cache-control"] == "no-cache"
    assert client.get("/static/vendor/three/three.module.js").headers["cache-control"] == "public, max-age=86400"
