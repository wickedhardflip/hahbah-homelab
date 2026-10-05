from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import FakeBuilder, make_user, sign_in

SNAP = {"schema": "homelab.snapshot/v1", "nodes": [], "links": [], "alerts": [],
        "sources": [{"id": "live", "label": "Every app's health check", "cadence": "every 30 s"},
                    {"id": "nas", "label": "Synology NAS over SSH", "cadence": "every minute · checked 20 s ago"}],
        "layout": {"lines": [{"id": "red", "name": "Red Line", "what": "Your domain, the internet and the home router", "links": []}]}}


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    (d / "hahbah-reel.mp4").write_bytes(b"\x00" * 5000)
    (d / "hahbah-reel.jpg").write_bytes(b"\xff\xd8poster")
    (d / "notes.txt").write_text("not media")
    return d


@pytest.fixture
def about_app(settings, media):
    return create_app(replace(settings, media_dir=media), builder=FakeBuilder(SNAP), collect=False)


def signed_in(app):
    make_user(app)
    c = TestClient(app)
    sign_in(c)
    return c


def test_about_needs_sign_in(about_app):
    assert TestClient(about_app).get("/about", follow_redirects=False).status_code == 303


def test_about_shows_the_reel_and_live_facts(about_app):
    body = signed_in(about_app).get("/about").text
    assert "<video" in body and 'src="/media/hahbah-reel.mp4?v=' in body and 'poster="/media/hahbah-reel.jpg?v=' in body
    assert "playsinline" in body
    assert "Synology NAS over SSH" in body and "every minute" in body      # live sources table
    assert "Red Line" in body and "Your domain, the internet and the home router" in body   # live line list


def test_about_without_the_video_still_renders(settings, tmp_path):
    app = create_app(replace(settings, media_dir=tmp_path / "nothing"), builder=FakeBuilder(SNAP), collect=False)
    body = signed_in(app).get("/about").text
    assert "<video" not in body and "Red Line" in body


def test_media_needs_sign_in(about_app):
    assert TestClient(about_app).get("/media/hahbah-reel.mp4", follow_redirects=False).status_code in (303, 401)


def test_media_serves_video_with_ranges(about_app):
    c = signed_in(about_app)
    full = c.get("/media/hahbah-reel.mp4")
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4" and len(full.content) == 5000
    part = c.get("/media/hahbah-reel.mp4", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100   # iPhone Safari needs range requests


def test_media_only_serves_known_media_names(about_app):
    c = signed_in(about_app)
    for path in ("/media/notes.txt", "/media/missing.mp4", "/media/..%2Fdata%2Fportal.db", "/media/HAHBAH-REEL.MP4"):
        assert c.get(path).status_code == 404, path


def test_dashboard_links_to_about(about_app):
    assert 'href="/about"' in signed_in(about_app).get("/").text
