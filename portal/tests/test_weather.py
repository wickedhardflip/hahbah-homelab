import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import settingsstore
from app.collectors import weather
from app.main import create_app
from app.models import User
from conftest import FakeBuilder, make_user, sign_in

FIX = Path(__file__).parent / "fixtures"
FORECAST = (FIX / "openmeteo_forecast.json").read_bytes()
GEO = (FIX / "openmeteo_geocode.json").read_bytes()
LOC = {"name": "Cambridge, Massachusetts, US", "lat": 42.3736, "lon": -71.1097}


def test_parse_forecast_gives_current_next_24_hours_and_7_days():
    w = weather.parse_forecast(json.loads(FORECAST), LOC)
    assert w["place"] == LOC["name"]
    assert w["current"]["temp"] in (76, 77) and w["current"]["day"] is True and w["current"]["code"] == 0
    assert len(w["hourly"]) == 24 and w["hourly"][0]["t"][:13] == "2026-10-02T10"
    assert set(w["hourly"][0]) == {"t", "temp", "pop", "code"}
    assert len(w["daily"]) == 7 and w["daily"][0]["d"] == "2026-10-02" and w["daily"][0]["hi"] >= w["daily"][0]["lo"]


def test_fetch_weather_asks_for_fahrenheit_mph_and_home_timezone():
    seen = []
    weather.fetch_weather(LOC, "America/New_York", get=lambda url: seen.append(url) or FORECAST)
    u = seen[0]
    assert u.startswith("https://api.open-meteo.com/v1/forecast?") and "temperature_unit=fahrenheit" in u
    assert "wind_speed_unit=mph" in u and "timezone=America%2FNew_York" in u and "latitude=42.3736" in u


@pytest.mark.parametrize("lat,lon,ok", [(35, -80, True), ("35.5", "-80.1", True), (91, 0, False), (0, 181, False),
                                        ("x", 1, False), (None, 1, False), (float("nan"), 1, False)])
def test_location_validation(lat, lon, ok):
    assert weather.valid_location(lat, lon) is ok


def test_read_location_accepts_a_good_file_and_defaults_on_anything_odd(tmp_path):
    f = tmp_path / "location.json"
    assert weather.read_location(f) == weather.DEFAULT   # missing
    weather.write_location(f, "Allston, Massachusetts, US", 42.3584, -71.1259)
    assert weather.read_location(f) == {"name": "Allston, Massachusetts, US", "lat": 42.3584, "lon": -71.1259}
    for bad in ('{"lat": 999, "lon": 1}', "not json", '{"lat": "a", "lon": 1}', "[]", '{"lat":1}'):
        f.write_text(bad)
        assert weather.read_location(f) == weather.DEFAULT
    f.write_text('{"name": "<script>x</script>' + "y" * 200 + '", "lat": 1, "lon": 2}')
    n = weather.read_location(f)["name"]
    assert "<" not in n and len(n) <= 80


def test_geocode_prefers_us_for_a_zip_and_uses_the_state_hint():
    asked = []
    get = lambda url: asked.append(url) or GEO
    assert weather.geocode("02139", get)["name"] == "Cambridge, Massachusetts, US"   # Spain is listed first
    assert weather.geocode("Cambridge, MA", get) == LOC
    assert "name=Cambridge" in asked[1] and "MA" not in asked[1]
    assert weather.geocode("Sedella", get)["name"] == "Sedella, Andalusia, ES"   # no hint: first result


def test_geocode_unknown_or_empty_place_is_none():
    assert weather.geocode("zzzz", lambda url: b'{"generationtime_ms":0.2}') is None
    assert weather.geocode("   ", lambda url: pytest.fail("no request for an empty query")) is None


# ---------- Settings + snapshot ----------

SNAP = {"schema": "homelab.snapshot/v1", "nodes": [], "links": [], "alerts": [], "layout": {"lines": [], "pos": {}, "via": {}},
        "edge": {}, "sources": [], "weather": {"place": "X", "current": {"temp": 70}}}


@pytest.fixture
def wapp(settings, tmp_path):
    d = tmp_path / "weather"
    d.mkdir()
    return create_app(replace(settings, weather_dir=d), builder=FakeBuilder(SNAP), collect=False)


def client(app, admin=True):
    make_user(app)
    with app.state.SessionLocal() as db:
        db.query(User).one().is_admin = admin
        db.commit()
    c = TestClient(app)
    sign_in(c)
    return c


def test_saving_a_place_stores_it_writes_the_file_and_shows_the_resolved_name(wapp, tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.geocode", lambda q: weather.geocode(q, lambda url: GEO))
    r = client(wapp).post("/settings", data={"action": "weather", "place": "02139"}, follow_redirects=True)
    assert "Weather location set to Cambridge, Massachusetts, US" in r.text
    assert json.loads((tmp_path / "weather" / "location.json").read_text()) == LOC
    with wapp.state.SessionLocal() as db:
        assert settingsstore.get(db, "weather_lat") == "42.3736"


def test_unknown_place_shows_an_error_and_keeps_the_old_location(wapp, tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.geocode", lambda q: None)
    r = client(wapp).post("/settings", data={"action": "weather", "place": "nowhere"}, follow_redirects=True)
    assert "find that place" in r.text and "Now: Cambridge, Massachusetts, US" in r.text
    assert not (tmp_path / "weather" / "location.json").exists()


def test_lookup_failure_keeps_the_old_location(wapp, monkeypatch):
    def boom(q):
        raise OSError("down")
    monkeypatch.setattr("app.main.geocode", boom)
    r = client(wapp).post("/settings", data={"action": "weather", "place": "x"}, follow_redirects=True)
    assert "didn&#39;t answer" in r.text or "didn't answer" in r.text


def test_weather_setting_is_admin_only(wapp):
    assert client(wapp, admin=False).post("/settings", data={"action": "weather", "place": "x"}).status_code == 403


def test_weather_setting_refuses_cross_site_posts(wapp):
    c = client(wapp)
    assert c.post("/settings", data={"action": "weather", "place": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_every_signed_in_user_sees_weather_in_the_snapshot(wapp):
    assert client(wapp, admin=False).get("/api/snapshot").json()["weather"]["place"] == "X"


def test_collector_has_a_weather_job_only_when_the_location_file_is_configured(tmp_path):
    from app.sidecar import build_jobs
    assert "weather" not in [j.name for j in build_jobs({}.get)]
    env = {"WEATHER_LOCATION": str(tmp_path / "location.json")}.get
    job = next(j for j in build_jobs(env) if j.name == "weather")
    assert job.interval_s == 900
