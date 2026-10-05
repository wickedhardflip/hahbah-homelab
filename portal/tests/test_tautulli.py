import json
import urllib.error
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.collectors import tautulli

FIX = Path(__file__).parent / "fixtures" / "tautulli"
TODAY = date(2026, 10, 1)


def load(name):
    return (FIX / name).read_bytes()


def fake_get(responses, seen=None):
    """responses: cmd -> bytes, exception, or callable(query). Records every query for assertions."""
    def get(url, timeout=3.0):
        q = parse_qs(urlparse(url).query)
        if seen is not None:
            seen.append(q)
        r = responses[q["cmd"][0]]
        if isinstance(r, Exception):
            raise r
        return r(q) if callable(r) else r
    return get


OK = {"get_activity": load("activity.json"), "get_history": load("history.json"),
      "get_home_stats": load("home_stats.json")}


def test_live_streams_are_summarised():
    s = tautulli.plex_stats("http://t:8181", "k", TODAY, get=fake_get(OK))
    assert s["available"] is True
    live = s["live"]
    assert live["stream_count"] == 2 and live["transcode_count"] == 1 and live["total_bandwidth_kbps"] == 12500
    a = live["streams"][0]
    assert (a["user"], a["title"], a["device"], a["decision"]) == ("alice", "Star Show - Pilot", "Living Room TV", "Transcode")
    assert a["reason"] == "Video codec not supported" and a["bandwidth_kbps"] == 4500 and a["progress_pct"] == 37
    assert a["thumb"] == "/library/metadata/100/thumb/1700000000"      # show poster for episodes
    assert live["streams"][1]["decision"] == "Direct Play"


def test_today_and_week_totals():
    seen = []
    s = tautulli.plex_stats("http://t:8181", "k", TODAY, get=fake_get(OK, seen))
    assert s["totals"]["week"] == {"plays": 3, "hours": 3.0}
    afters = sorted(q["after"][0] for q in seen if q["cmd"][0] == "get_history")
    assert afters == ["2026-09-25", "2026-10-01"]


def test_top_users_and_titles():
    s = tautulli.plex_stats("http://t:8181", "k", TODAY, get=fake_get(OK))
    assert s["top_users"][0] == {"user": "Alice", "user_id": 11, "plays": 6, "hours": 6.0}
    assert [t["title"] for t in s["top_titles"]] == ["Star Show", "Big Movie"]    # by plays, movies + tv merged
    assert s["top_titles"][0]["thumb"] == "/library/metadata/100/thumb/1700000000"


def test_activity_tolerates_strings_and_nulls():
    odd = json.dumps({"response": {"result": "success", "data": {"stream_count": None, "total_bandwidth": None}}}).encode()
    s = tautulli.plex_stats("http://t:8181", "k", TODAY, get=fake_get(dict(OK, get_activity=odd)))
    assert s["live"] == {"stream_count": 0, "transcode_count": 0, "total_bandwidth_kbps": 0, "streams": []}


def test_history_paging_is_capped():
    def endless(q):
        rows = [{"date": 1, "play_duration": 60}] * int(q["length"][0])
        return json.dumps({"response": {"result": "success", "data": {"recordsFiltered": 10**6, "data": rows}}}).encode()
    seen = []
    s = tautulli.plex_stats("http://t:8181", "k", TODAY, get=fake_get(dict(OK, get_history=endless), seen))
    assert s["totals"]["week"]["plays"] == 2000
    assert sum(1 for q in seen if q["cmd"][0] == "get_history") <= 2 * 4      # 500 per page, 2000 cap, two ranges


@pytest.mark.parametrize("exc,words", [
    (urllib.error.HTTPError("http://t:8181/api/v2?apikey=SECRET", 401, "Unauthorized", {}, None), "refused the API key"),
    (urllib.error.URLError("timed out"), "didn't answer"),
    (TimeoutError(), "didn't answer"),
])
def test_failures_are_plain_words_without_the_key(exc, words):
    s = tautulli.plex_stats("http://t:8181", "SECRET", TODAY, get=fake_get(dict(OK, get_activity=exc)))
    assert s["available"] is False and words in s["reason"]
    assert "SECRET" not in s["reason"] and "apikey" not in s["reason"]


def test_non_json_and_error_results():
    s = tautulli.plex_stats("http://t:8181", "k", TODAY, get=fake_get(dict(OK, get_activity=b"<html>")))
    assert s == {"available": False, "reason": "Tautulli sent something unexpected"}
    bad = json.dumps({"response": {"result": "error", "message": "Invalid apikey"}}).encode()
    s = tautulli.plex_stats("http://t:8181", "k", TODAY, get=fake_get(dict(OK, get_activity=bad)))
    assert s == {"available": False, "reason": "Tautulli said: Invalid apikey"}


def test_not_set_up_without_url_or_key():
    assert tautulli.plex_stats("", "k", TODAY) == {"available": False, "reason": "not set up"}
    assert tautulli.plex_stats("http://t:8181", "", TODAY) == {"available": False, "reason": "not set up"}


def test_user_history_rows():
    rows = tautulli.user_history("http://t:8181", "k", 11, get=fake_get({"get_history": load("user_history.json")}))
    assert rows == [{"title": "Star Show - Pilot", "date": 1790870000, "minutes": 30, "device": "Living Room TV",
                     "decision": "Transcode"}]


def test_image_passes_path_and_width():
    calls = []

    def raw(url, timeout=3.0):
        calls.append(parse_qs(urlparse(url).query))
        return b"\x89PNG", "image/png"
    body, ctype = tautulli.image("http://t:8181", "k", "/library/metadata/1/thumb/2", 120, get_raw=raw)
    assert (body, ctype) == (b"\x89PNG", "image/png")
    assert calls[0]["cmd"] == ["pms_image_proxy"] and calls[0]["img"] == ["/library/metadata/1/thumb/2"]
    assert calls[0]["width"] == ["120"]


def test_today_is_the_home_date_not_utc():
    from datetime import datetime, timezone
    late_evening_et = datetime(2026, 10, 2, 1, 30, tzinfo=timezone.utc)      # 9:30 pm Oct 1 in Boston
    assert tautulli.local_today("America/New_York", now=late_evening_et) == date(2026, 10, 1)
    assert tautulli.local_today("UTC", now=late_evening_et) == date(2026, 10, 2)
