import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import FakeBuilder, make_user, sign_in

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "portal" / "app" / "static"
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture
def fp_app(settings, media):
    return create_app(replace(settings, media_dir=media), builder=FakeBuilder(), collect=False)


def signed_in(app, admin=True):
    from app.models import User
    make_user(app)
    with app.state.SessionLocal() as db:
        db.query(User).update({"is_admin": admin})
        db.commit()
    c = TestClient(app)
    sign_in(c)
    return c


def test_floorplan_needs_sign_in(fp_app):
    c = TestClient(fp_app)
    assert c.get("/floorplan", follow_redirects=False).status_code == 303
    assert c.get("/floorplan/image", follow_redirects=False).status_code in (303, 401)


def test_floorplan_page_without_image_shows_empty_state(fp_app):
    body = signed_in(fp_app).get("/floorplan").text
    assert "Add floorplan.jpg to the media folder" in body and 'id="fpImg"' not in body


def test_non_admin_empty_state_and_no_edit_flag(fp_app, media):
    (media / "floorplan.jpg").write_bytes(b"\xff\xd8img")
    body = signed_in(fp_app, admin=False).get("/floorplan").text
    assert "window.HOMELAB_ADMIN = false" in body and "Edit layout" not in body
    assert "Choose Edit layout" not in body


def test_admin_flag_in_page(fp_app, media):
    (media / "floorplan.jpg").write_bytes(b"\xff\xd8img")
    body = signed_in(fp_app).get("/floorplan").text
    assert "window.HOMELAB_ADMIN = true" in body and 'src="/floorplan/image"' in body


def test_no_image_is_404_then_served_with_cache_header(fp_app, media):
    c = signed_in(fp_app)
    assert c.get("/floorplan/image").status_code == 404
    (media / "floorplan.jpg").write_bytes(b"\xff\xd8img")
    r = c.get("/floorplan/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "private, max-age=3600"


def test_menu_links_to_floorplan(fp_app):
    assert 'href="/floorplan"' in signed_in(fp_app).get("/").text


def test_fixtures_match_the_contract():
    plugs = json.loads((FIX / "plugs_api.json").read_text())
    assert plugs["configured"] is True and len(plugs["plugs"]) == 3
    assert {"id", "name", "label", "model", "online", "on", "away", "schedules_enabled", "schedules", "last_cmd", "last_sched", "next"} <= set(plugs["plugs"][0])
    assert [p["online"] for p in plugs["plugs"]] == [True, True, False]
    fp = json.loads((FIX / "floorplan_api.json").read_text())
    assert set(fp) == {"layout", "available", "image"} and all({"id", "kind", "label", "detail"} <= set(a) for a in fp["available"])


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("name", ["plugs.js", "floorplan.js", "noc.js"])
def test_js_parses(name):
    assert subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True).returncode == 0


def test_export_never_ships_the_drawing():
    sys.path.insert(0, str(ROOT / "tools"))
    import export_public
    assert export_public.excluded("images/floorplan.jpg") and export_public.excluded("portal/media/floorplan.jpg")


def test_non_admin_sees_plain_empty_state(fp_app):
    assert "No floorplan yet" in signed_in(fp_app, admin=False).get("/floorplan").text
