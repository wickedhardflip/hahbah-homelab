import copy
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import FakeBuilder, make_user, sign_in

PLEX = {"available": True,
        "live": {"stream_count": 1, "transcode_count": 1, "total_bandwidth_kbps": 4500,
                 "streams": [{"user": "alice-viewer", "user_id": 11, "title": "Secret Show Title", "media_type": "episode",
                              "device": "Living Room TV", "decision": "Transcode", "reason": "", "quality": "720p",
                              "bandwidth_kbps": 4500, "progress_pct": 37, "thumb": "/library/metadata/100/thumb/1"}]},
        "totals": {"today": {"plays": 1, "hours": 0.5}, "week": {"plays": 3, "hours": 3.0}},
        "top_users": [{"user": "alice-viewer", "user_id": 11, "plays": 6, "hours": 6.0}],
        "top_titles": [{"title": "Secret Show Title", "plays": 5, "thumb": "/library/metadata/100/thumb/1"}]}
SNAP = {"schema": "homelab.snapshot/v1", "links": [], "alerts": [], "plex": PLEX,
        "nodes": [{"id": "tautulli", "kind": "app", "label": "Tautulli", "url": "https://tautulli.hahbah.com", "admin_only": True},
                  {"id": "jobs", "kind": "app", "label": "Example App", "url": "https://jobs.hahbah.com"}]}
PRIVATE = ("alice-viewer", "Secret Show Title", "/library/metadata/100", "tautulli.hahbah.com")


@pytest.fixture
def plex_app(settings):
    s = replace(settings, tautulli_url="http://tautulli:8181", tautulli_api_key="FAKE-k")
    return create_app(s, builder=FakeBuilder(copy.deepcopy(SNAP)), collect=False)


def as_user(app, admin):
    from app.models import User
    make_user(app)
    with app.state.SessionLocal() as db:
        db.query(User).one().is_admin = admin
        db.commit()
    c = TestClient(app)
    sign_in(c)
    return c


def test_non_admin_snapshot_has_counts_but_no_detail(plex_app):
    body = as_user(plex_app, False).get("/api/snapshot").text
    assert '"stream_count":1' in body.replace(" ", "") and '"week"' in body
    assert [p for p in PRIVATE if p in body] == []


def test_dashboard_html_is_redacted_for_non_admins(plex_app):
    body = as_user(plex_app, False).get("/").text
    assert [p for p in PRIVATE if p in body] == [] and "window.HOMELAB_ADMIN = false" in body


def test_admin_sees_everything(plex_app):
    c = as_user(plex_app, True)
    body = c.get("/api/snapshot").text
    assert all(p in body for p in PRIVATE)
    assert "window.HOMELAB_ADMIN = true" in c.get("/").text


def test_store_is_not_mutated_by_redaction(plex_app):
    as_user(plex_app, False).get("/api/snapshot")
    assert plex_app.state.store.current()["plex"]["live"]["streams"]


def test_admin_apps_hidden_from_non_admin_app_list(plex_app, settings):
    settings.apps_file.write_text(
        "apps:\n  - {id: jobs, name: Example App, subdomain: jobs, host: central, port: 8080, health: /login, lan_url: 'http://x'}\n"
        "  - {id: tautulli, name: Tautulli, subdomain: tautulli, host: central, port: 8181, health: /status, lan_url: '',"
        " upstream: 'tautulli:8181', admin: true}\n")
    assert [a["id"] for a in as_user(plex_app, False).get("/api/apps").json()["apps"]] == ["jobs"]


def test_verify_admin_role(plex_app):
    anon = TestClient(plex_app)
    r = anon.get("/auth/verify?role=admin", headers={"X-Forwarded-Host": "tautulli.hahbah.com", "X-Forwarded-Uri": "/"},
                 follow_redirects=False)
    assert r.status_code == 302 and "/login?next=" in r.headers["location"]
    assert as_user(plex_app, False).get("/auth/verify?role=admin").status_code == 403


def test_verify_admin_role_lets_admin_through(plex_app):
    r = as_user(plex_app, True).get("/auth/verify?role=admin")
    assert r.status_code == 200 and r.headers["Remote-User"] == "alex"


def test_plex_endpoints_are_admin_only(plex_app):
    c = as_user(plex_app, False)
    assert c.get("/api/plex/user/11").status_code == 403
    assert c.get("/api/plex/thumb?path=/library/metadata/1/thumb/2").status_code == 403


def test_user_history_for_admin(plex_app):
    fx = (Path(__file__).parent / "fixtures/tautulli/user_history.json").read_bytes()
    plex_app.state.tautulli_get = lambda url, timeout=3.0: fx
    r = as_user(plex_app, True).get("/api/plex/user/11")
    assert r.status_code == 200 and r.json()["plays"][0]["title"] == "Star Show - Pilot"


@pytest.mark.parametrize("path", ["http://evil/x.png", "/library/metadata/1/thumb/2?x=http://evil", "/library/../etc/passwd",
                                  "/library/metadata/1/thumb/2/../../x", "/library/metadata/abc/thumb/2", ""])
def test_thumb_rejects_bad_paths(plex_app, path):
    plex_app.state.tautulli_get_raw = lambda url, timeout=3.0: (b"img", "image/jpeg")
    assert as_user(plex_app, True).get("/api/plex/thumb", params={"path": path}).status_code == 400


def test_thumb_for_admin_clamps_width_and_caches(plex_app):
    seen = []

    def raw(url, timeout=3.0):
        seen.append(url)
        return b"img", "image/jpeg"
    plex_app.state.tautulli_get_raw = raw
    r = as_user(plex_app, True).get("/api/plex/thumb", params={"path": "/library/metadata/1/thumb/2", "w": 9999})
    assert r.status_code == 200 and r.content == b"img" and r.headers["content-type"] == "image/jpeg"
    assert "private" in r.headers["cache-control"] and "width=400" in seen[0]


def test_dashboard_ships_the_drive_in(plex_app):
    body = as_user(plex_app, False).get("/").text
    assert 'kind: "drivein"' in body and 'g.userData.id = "plex"' in body and "paintDriveIn()" in body


def test_tautulli_station_shows_the_plex_card(plex_app):
    body = as_user(plex_app, True).get("/").text
    assert 'if (id === "plex" || id === "tautulli") plexCard(id);' in body


def test_non_admins_dont_see_admin_only_names_in_the_dns_list():
    from app.views import snapshot_for
    snap = {"nodes": [{"id": "tautulli", "admin_only": True, "url": "https://tautulli.hahbah.com"},
                      {"id": "jobs", "url": "https://jobs.hahbah.com"}],
            "edge": {"dns": {"expected": [{"name": "jobs.hahbah.com"}, {"name": "tautulli.hahbah.com"}],
                             "wrong": ["tautulli.hahbah.com"], "missing": [], "via_eero_ok": True}}}
    out = snapshot_for(snap, False)
    assert [e["name"] for e in out["edge"]["dns"]["expected"]] == ["jobs.hahbah.com"] and out["edge"]["dns"]["wrong"] == []
    assert len(snapshot_for(snap, True)["edge"]["dns"]["expected"]) == 2
