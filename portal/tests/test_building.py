import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes_building import PLACEHOLDER, clean
from conftest import FakeBuilder, make_user, sign_in

APP = Path(__file__).resolve().parents[1] / "app"
GOOD = {"caption": "Test block", "levels": 3, "unit_level": 2, "footprint": [[0, 0], [10, 0], [10, 5], [0, 5]], "unit": [1, 1, 9, 4]}


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture
def client(settings, media):
    app = create_app(replace(settings, media_dir=media), builder=FakeBuilder(), collect=False)
    make_user(app)
    c = TestClient(app)
    sign_in(c)
    return c


def test_signed_out_is_refused(settings, media):
    c = TestClient(create_app(replace(settings, media_dir=media), builder=FakeBuilder(), collect=False), follow_redirects=False)
    assert c.get("/building").status_code in (302, 303, 401)
    assert c.get("/api/building").status_code in (302, 303, 401)


def test_page_loads_with_import_map(client):
    r = client.get("/building")
    assert r.status_code == 200 and "three.module.js" in r.text and "/static/building.js" in r.text
    assert "BUILDING_HAS_IMAGE = false" in r.text


def test_placeholder_without_file(client):
    d = client.get("/api/building").json()
    assert d["placeholder"] is True and d["levels"] == 3 and d["has_image"] is False


def test_stored_building_and_image_flag(client, media):
    (media / "building.json").write_text(json.dumps(GOOD), encoding="utf-8")
    (media / "floorplan.jpg").write_bytes(b"x")
    d = client.get("/api/building").json()
    assert d["placeholder"] is False and d["caption"] == "Test block" and d["footprint"] == GOOD["footprint"] and d["has_image"] is True


@pytest.mark.parametrize("bad", [None, [], {}, {**GOOD, "levels": 0}, {**GOOD, "unit_level": 4}, {**GOOD, "footprint": [[0, 0]]},
                                 {**GOOD, "footprint": [[0, 0], [1, 1], ["a", 2]]}, {**GOOD, "unit": [1, 2]}, {**GOOD, "levels": True}])
def test_bad_building_json_falls_back(bad):
    assert clean(bad) == PLACEHOLDER


def test_no_private_building_details_in_code():
    for f in ("routes_building.py", "static/building.js", "static/building.css", "templates/building.html"):
        text = (APP / f).read_text(encoding="utf-8").lower()
        assert not any(w in text for w in ("king" + "sley", "14" + "45", "fort" + " mill"))
