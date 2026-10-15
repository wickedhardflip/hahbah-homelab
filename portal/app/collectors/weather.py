"""Weather from Open-Meteo (no key): the collector fetches the forecast, the portal geocodes a place name for Settings.
Only the portal's Settings page chooses the place; it writes lat/lon to a small file the collector reads (see read_location)."""
import json
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
DEFAULT = {"name": "Cambridge, Massachusetts, US", "lat": 42.3736, "lon": -71.1097}


class WeatherError(Exception):
    pass


def default_get(url: str, timeout: float = 8.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(1_000_000)


def valid_location(lat, lon) -> bool:
    try:
        return -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180 and all(map(lambda v: v == v, (float(lat), float(lon))))
    except (TypeError, ValueError):
        return False


def clean_name(name) -> str:
    return re.sub(r"[^\w ,.'()-]", "", str(name or ""))[:80].strip()


def read_location(path) -> dict:
    """The portal-written location file, else the default. Anything odd (not JSON, not two in-range floats) means the default."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if valid_location(doc["lat"], doc["lon"]):
            return {"name": clean_name(doc.get("name")) or DEFAULT["name"], "lat": float(doc["lat"]), "lon": float(doc["lon"])}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return dict(DEFAULT)


def write_location(path, name: str, lat: float, lon: float) -> None:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"name": name, "lat": lat, "lon": lon}), encoding="utf-8")
    tmp.replace(p)


def parse_forecast(doc: dict, loc: dict) -> dict:
    """Open-Meteo forecast JSON -> {place, current, hourly (next 24 h), daily (7 days)}; local ISO times as given."""
    cur, hr, dy = doc["current"], doc["hourly"], doc["daily"]
    start = cur["time"][:13]
    times = hr["time"]
    i = next((k for k, t in enumerate(times) if t[:13] >= start), 0)
    hourly = [{"t": times[k], "temp": round(hr["temperature_2m"][k]), "pop": hr["precipitation_probability"][k],
               "code": hr["weather_code"][k]} for k in range(i, min(i + 24, len(times)))]
    d = doc["daily"]
    daily = [{"d": d["time"][k], "hi": round(d["temperature_2m_max"][k]), "lo": round(d["temperature_2m_min"][k]),
              "pop": d["precipitation_probability_max"][k], "code": d["weather_code"][k]} for k in range(min(7, len(d["time"])))]
    return {"place": loc["name"], "current": {"temp": round(cur["temperature_2m"]), "code": cur["weather_code"],
                                              "day": bool(cur.get("is_day", 1)), "t": cur["time"]},
            "hourly": hourly, "daily": daily}


def fetch_weather(loc: dict, tz: str, get=default_get) -> dict:
    q = urlencode({"latitude": loc["lat"], "longitude": loc["lon"], "timezone": tz, "forecast_days": 7,
                   "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                   "current": "temperature_2m,weather_code,is_day",
                   "hourly": "temperature_2m,precipitation_probability,weather_code",
                   "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
    return parse_forecast(json.loads(get(f"{FORECAST}?{q}")), loc)


def geocode(query: str, get=default_get) -> dict | None:
    """City or ZIP -> {name, lat, lon}, or None when nothing matches. "Cambridge, MA" searches "Cambridge" and prefers a
    result whose state/country mentions the rest; a 5-digit ZIP prefers US places."""
    q = re.sub(r"\s+", " ", (query or "")).strip()[:80]
    if not q:
        return None
    head, _, hint = (s.strip() for s in q.partition(","))
    zip_ = bool(re.fullmatch(r"\d{5}", head))
    doc = json.loads(get(f"{GEOCODE}?" + urlencode({"name": head, "count": 10, "language": "en", "format": "json"})))
    results = [r for r in doc.get("results") or [] if valid_location(r.get("latitude"), r.get("longitude"))]
    if not results:
        return None
    pick = results[0]
    if zip_:
        pick = next((r for r in results if r.get("country_code") == "US"), pick)
    elif hint:
        h = hint.lower()
        pick = next((r for r in results if _hint(r, h)), pick)
    name = ", ".join(p for p in (pick.get("name"), pick.get("admin1"), pick.get("country_code")) if p)
    return {"name": clean_name(name), "lat": round(float(pick["latitude"]), 4), "lon": round(float(pick["longitude"]), 4)}


def _hint(r: dict, h: str) -> bool:
    """Does the part after the comma match the result's state or country? "ma" matches Massachusetts (initials, or first
    two letters of a one-word state), "us" the country code."""
    if h in " ".join(str(r.get(k, "")) for k in ("admin1", "country")).lower() or h == str(r.get("country_code", "")).lower():
        return True
    words = str(r.get("admin1", "")).lower().split()
    return len(h) == 2 and bool(words) and h == ("".join(w[0] for w in words) if len(words) > 1 else words[0][:2])
