"""Plex activity and history from Tautulli's API (v2). The API key travels as a query parameter,
so request URLs are never logged or put in error messages."""
import json
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

PAGE, CAP = 500, 2000
DECISIONS = {"direct play": "Direct Play", "copy": "Direct Stream", "transcode": "Transcode"}


class TautulliError(Exception):
    pass


def default_get(url: str, timeout: float = 3.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def default_get_raw(url: str, timeout: float = 3.0) -> tuple:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "application/octet-stream")


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _url(base: str, key: str, cmd: str, **params) -> str:
    return f"{base}/api/v2?" + urlencode({"apikey": key, "cmd": cmd, **params})


def _call(get, base, key, cmd, **params):
    try:
        body = get(_url(base, key, cmd, **params), timeout=3.0)
    except urllib.error.HTTPError as e:
        raise TautulliError("Tautulli refused the API key" if e.code in (401, 403) else f"Tautulli answered HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TautulliError("Tautulli didn't answer") from None
    try:
        resp = json.loads(body)["response"]
    except (ValueError, KeyError, TypeError):
        raise TautulliError("Tautulli sent something unexpected") from None
    if resp.get("result") != "success":
        raise TautulliError(f"Tautulli said: {resp.get('message') or 'error'}")
    return resp.get("data")


def _decision(s: dict) -> str:
    raw = s.get("transcode_decision") or ""
    return DECISIONS.get(raw.lower(), raw.title())


def _live(data) -> dict:
    data = data or {}
    streams = []
    for s in data.get("sessions") or []:
        streams.append({
            "user": s.get("friendly_name") or s.get("user") or "?", "user_id": _int(s.get("user_id")),
            "title": s.get("full_title") or s.get("title") or "?", "media_type": s.get("media_type") or "",
            "device": s.get("player") or s.get("product") or "?", "decision": _decision(s),
            "reason": s.get("transcode_reason") or "", "quality": s.get("quality_profile") or "",
            "bandwidth_kbps": _int(s.get("bandwidth")), "progress_pct": _int(s.get("progress_percent")),
            "thumb": (s.get("grandparent_thumb") if s.get("media_type") == "episode" else None) or s.get("thumb") or "",
        })
    return {"stream_count": _int(data.get("stream_count")), "transcode_count": _int(data.get("stream_count_transcode")),
            "total_bandwidth_kbps": _int(data.get("total_bandwidth")), "streams": streams}


def _totals_since(get, base, key, after: date) -> dict:
    plays, seconds, start = 0, 0, 0
    while start < CAP:
        data = _call(get, base, key, "get_history", after=after.isoformat(), start=start, length=PAGE) or {}
        rows = (data.get("data") or [])[: CAP - start]
        plays += len(rows)
        seconds += sum(_int(r.get("play_duration")) for r in rows)
        start += PAGE
        if len(rows) < PAGE or start >= _int(data.get("recordsFiltered")):
            break
    return {"plays": plays, "hours": round(seconds / 3600, 1)}


def _home(data) -> tuple:
    stats = {s.get("stat_id"): s.get("rows") or [] for s in (data or [])}
    users = [{"user": r.get("friendly_name") or r.get("user") or "?", "user_id": _int(r.get("user_id")),
              "plays": _int(r.get("total_plays")), "hours": round(_int(r.get("total_duration")) / 3600, 1)}
             for r in stats.get("top_users", [])]
    titles = [{"title": r.get("title") or "?", "plays": _int(r.get("total_plays")),
               "thumb": r.get("grandparent_thumb") or r.get("thumb") or ""}
              for r in stats.get("top_movies", []) + stats.get("top_tv", [])]
    titles.sort(key=lambda t: -t["plays"])
    return users[:5], titles[:5]


def local_today(tz: str, now: datetime | None = None) -> date:
    """Today's date at home: Tautulli's `after=` dates are in the home time zone, the portal container is UTC."""
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date()


def plex_stats(base_url: str, api_key: str, today: date, get=default_get) -> dict:
    if not base_url or not api_key:
        return {"available": False, "reason": "not set up"}
    try:
        live = _live(_call(get, base_url, api_key, "get_activity"))
        totals = {"today": _totals_since(get, base_url, api_key, today),
                  "week": _totals_since(get, base_url, api_key, today - timedelta(days=6))}
        users, titles = _home(_call(get, base_url, api_key, "get_home_stats", time_range=7, stats_type="plays", stats_count=5))
    except TautulliError as e:
        return {"available": False, "reason": str(e)}
    return {"available": True, "live": live, "totals": totals, "top_users": users, "top_titles": titles}


def user_history(base_url: str, api_key: str, user_id: int, get=default_get) -> list:
    data = _call(get, base_url, api_key, "get_history", user_id=user_id, length=20) or {}
    return [{"title": r.get("full_title") or "?", "date": _int(r.get("date")), "minutes": _int(r.get("play_duration")) // 60,
             "device": r.get("player") or "?", "decision": _decision(r)} for r in data.get("data") or []]


def image(base_url: str, api_key: str, path: str, width: int, get_raw=default_get_raw) -> tuple:
    try:
        return get_raw(_url(base_url, api_key, "pms_image_proxy", img=path, width=width, fallback="poster"), timeout=3.0)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TautulliError("Tautulli didn't answer") from None
