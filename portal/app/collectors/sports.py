"""The three stadium cards: a ballpark team (MLB Stats API) and an arena and bus-stop team (ESPN's public feeds: NBA, WNBA, NHL,
NFL, MLS), trimmed to what the cards show. Settings picks the teams (see sportsteams); the defaults are Red Sox, Celtics, Hornets.
No keys. Each slot is fetched on its own: one source failing leaves the other intact, and a failed refresh keeps the
last good copy for up to an hour (after that the card says "Scores unavailable")."""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from .sportsteams import DEFAULT_PICKS, LEAGUES, SLOTS, split_pick, team_meta

MLB = "https://statsapi.mlb.com/api/v1"
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_V2 = "https://site.api.espn.com/apis/v2/sports"
LOGO_DIR = {"nba": "nba", "wnba": "wnba", "nhl": "nhl", "nfl": "nfl", "mls": "soccer"}   # where ESPN keeps each league's logos
REFRESH_S, LIVE_REFRESH_S, KEEP_S = 900, 120, 3600


def default_get(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={"User-Agent": "hahbah-portal/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


LOGO_HOSTS = ("a.espncdn.com", "www.mlbstatic.com")   # the only hosts the cards may load images from


def mlb_logo(team_id) -> str | None:
    return f"https://www.mlbstatic.com/team-logos/{team_id}.svg" if isinstance(team_id, int) else None


def espn_logo(team: dict, league: str = "nba") -> str | None:
    """ESPN's own logo URL when the feed has one, else its standard path for the team (MLS logos are named by team id)."""
    url = team.get("logo")
    if isinstance(url, str) and url.startswith("https://") and url.split("/")[2] in LOGO_HOSTS:
        return url
    name = str(team.get("id") or "") if league == "mls" else team.get("abbreviation")
    return f"https://a.espncdn.com/i/teamlogos/{LOGO_DIR[league]}/500/{name.lower()}.png" if isinstance(name, str) and name.isalnum() else None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _result(us, them):
    return None if us is None or them is None else ("W" if us > them else "L" if us < them else "T")


def _pack(games: list) -> tuple:
    """games: dicts with date (ISO UTC), state pre|live|post, opp, home, us, them, result, detail -> (last 5, next, live)."""
    games = sorted(games, key=lambda g: g["date"] or "")
    last = [{k: g[k] for k in ("date", "opp", "opp_logo", "home", "us", "them", "result")} for g in games if g["state"] == "post"][-5:]
    live = next(({k: g[k] for k in ("opp", "opp_logo", "home", "us", "them", "detail")} for g in games if g["state"] == "live"), None)
    nxt = next(({k: g[k] for k in ("date", "opp", "opp_logo", "home")} for g in games if g["state"] == "pre"), None)
    return last, nxt, live


# ---------- MLB (the ballpark) ----------

def parse_mlb_games(doc: dict, team_id: int = 111) -> list:
    out = []
    for d in doc.get("dates") or []:
        for g in d.get("games") or []:
            st = g.get("status") or {}
            detailed = st.get("detailedState", "")
            if g.get("gameType") == "E" or any(w in detailed for w in ("Postponed", "Cancelled", "Suspended")):   # E = exhibition vs a college
                continue
            tm = g.get("teams") or {}
            home = ((tm.get("home") or {}).get("team") or {}).get("id") == team_id
            us, them = tm.get("home" if home else "away") or {}, tm.get("away" if home else "home") or {}
            ls = g.get("linescore") or {}
            state = {"Final": "post", "Live": "live"}.get(st.get("abstractGameState"), "pre")
            u, t = _int(us.get("score")), _int(them.get("score"))
            if state == "post" and (u is None or t is None):
                continue
            out.append({"date": g.get("gameDate"), "state": state, "opp": (them.get("team") or {}).get("name", "?"),
                        "opp_logo": mlb_logo((them.get("team") or {}).get("id")), "home": home, "us": u, "them": t, "result": _result(u, t),
                        "detail": " ".join(x for x in (ls.get("inningHalf"), ls.get("currentInningOrdinal")) if x) or detailed})
    return out


def parse_mlb_standings(doc: dict, team_id: int = 111, title: str = "AL East") -> dict | None:
    """The division table that holds `team_id`, whichever league's standings the document came from."""
    for rec in doc.get("records") or []:
        recs = rec.get("teamRecords") or []
        if not any((r.get("team") or {}).get("id") == team_id for r in recs):
            continue
        rows = [{"name": (r.get("team") or {}).get("name", "?"), "w": r["leagueRecord"]["wins"], "l": r["leagueRecord"]["losses"],
                 "gb": str(r.get("gamesBack", "-")), "logo": mlb_logo((r.get("team") or {}).get("id")), "us": (r.get("team") or {}).get("id") == team_id}
                for r in recs]
        return {"title": title, "cols": [list(c) for c in LEAGUES["mlb"].cols], "rows": rows} if rows else None
    return None


def fetch_mlb(get, now: datetime, team_id: int) -> dict:
    d = now.astimezone(timezone.utc).date()
    base = f"{MLB}/schedule?sportId=1&teamId={team_id}&hydrate=linescore"
    games = parse_mlb_games(get(f"{base}&startDate={d - timedelta(days=21)}&endDate={d + timedelta(days=21)}"), team_id)
    if not any(g["state"] != "post" for g in games):   # off season: look ahead for opening day
        games += parse_mlb_games(get(f"{base}&startDate={d}&endDate={d + timedelta(days=300)}"), team_id)
    last, nxt, live = _pack(games)
    try:   # both leagues: the team's own division is found in whichever one holds it
        stand = parse_mlb_standings(get(f"{MLB}/standings?leagueId=103,104&season={d.year}&standingsTypes=regularSeason"),
                                    team_id, LEAGUES["mlb"].teams[str(team_id)][3])
    except Exception:  # noqa: BLE001 (standings are optional; the rest of the card still shows)
        stand = None
    return {"last": last, "next": nxt, "live": live, "standings": stand, "logo": mlb_logo(team_id)}


# ---------- NBA (the arena and the bus stop) ----------

def parse_espn_games(doc: dict, abbr: str = "BOS", league: str = "nba") -> list:
    out = []
    for ev in doc.get("events") or []:
        c = (ev.get("competitions") or [{}])[0]
        stt = (c.get("status") or {}).get("type") or {}
        if stt.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
            continue
        comp = {x.get("homeAway"): x for x in c.get("competitors") or []}
        home = ((comp.get("home") or {}).get("team") or {}).get("abbreviation") == abbr
        us, them = comp.get("home" if home else "away") or {}, comp.get("away" if home else "home") or {}
        state = {"post": "post", "in": "live"}.get(stt.get("state"), "pre")

        def score(x):
            s = x.get("score")
            return _int(s.get("value") if isinstance(s, dict) else s)
        u, t = score(us), score(them)
        if state == "post" and (u is None or t is None):
            continue
        out.append({"id": ev.get("id"), "date": ev.get("date"), "state": state, "home": home, "us": u, "them": t,
                    "opp": (them.get("team") or {}).get("displayName", "?"),
                    "opp_logo": espn_logo(them.get("team") or {}, league), "us_logo": espn_logo(us.get("team") or {}, league), "result": _result(u, t),
                    "detail": stt.get("shortDetail") or stt.get("detail") or ""})
    return out


_STAT = {"w": ("wins",), "l": ("losses",), "gb": ("gamesBehind",), "otl": ("otLosses", "overtimeLosses"), "t": ("ties",), "pts": ("points",)}


def parse_espn_standings(doc: dict, abbr: str = "BOS", conf: str = "East", league: str = "nba") -> dict | None:
    """The conference table (`conf` is its short title, e.g. East or AFC) with the columns this league reads by."""
    L = LEAGUES[league]
    for c in doc.get("children") or []:
        if c.get("name") != L.groups.get(conf):
            continue
        rows = []
        for e in (c.get("standings") or {}).get("entries") or []:
            s = {x.get("name"): x.get("displayValue") for x in e.get("stats") or []}
            tm = e.get("team") or {}
            row = {"name": tm.get("shortDisplayName") or tm.get("displayName", "?"), "logo": espn_logo(tm, league),
                   "us": tm.get("abbreviation") == abbr}
            for key, _ in L.cols:
                v = next((s[n] for n in _STAT[key] if s.get(n) is not None), None)
                row[key] = (v or "-") if key == "gb" else (_int(v) or 0)
            row["_seed"] = _int(s.get("playoffSeed")) or _int(s.get("rank")) or 99
            rows.append(row)
        rows.sort(key=lambda r: r["_seed"])
        for r in rows:
            r.pop("_seed")
        return {"title": conf, "cols": [list(k) for k in L.cols], "rows": rows} if rows else None
    return None


def fetch_espn(get, now: datetime, league: str, key: str) -> dict:
    """One team's card from ESPN's schedule + its own conference's standings. `key` is the team's ESPN abbreviation."""
    L = LEAGUES[league]
    name, _short, _city, conf, _c1, _c2, espn_id = L.teams[key]
    url = f"{ESPN_SITE}/{L.path}/teams/{espn_id or key.lower()}/schedule"
    games = {g["id"]: g for g in parse_espn_games(get(url), key, league)}
    if sum(g["state"] == "post" for g in games.values()) < L.min_recent:   # early in the season: pull in last season's tail
        yr = (now.year if now.month >= 7 else now.year - 1) if L.season == "end" else now.year - 1
        for st in (2, 3):
            try:
                games.update({g["id"]: g for g in parse_espn_games(get(f"{url}?season={yr}&seasontype={st}"), key, league)})
            except Exception:  # noqa: BLE001 (optional history)
                pass
    last, nxt, live = _pack(list(games.values()))
    try:
        stand = parse_espn_standings(get(f"{ESPN_V2}/{L.path}/standings"), key, conf, league)
    except Exception:  # noqa: BLE001
        stand = None
    logo = next((g["us_logo"] for g in games.values() if g.get("us_logo")), None) or espn_logo({"abbreviation": key.lower(), "id": espn_id}, league)
    return {"last": last, "next": nxt, "live": live, "standings": stand, "logo": logo}


# ---------- the job ----------

def _fetch(pick: str, get, now: datetime) -> dict:
    league, key = split_pick(pick)
    return fetch_mlb(get, now, int(key)) if league == "mlb" else fetch_espn(get, now, league, key)


class Sports:
    """Called every ~2 minutes by the collector; hits each API every 15 min, or every 2 min while that team's game is live.
    `picks` says which team each slot shows; changing a pick starts that slot over."""

    def __init__(self, get=default_get, clock=time.monotonic, picks=lambda: dict(DEFAULT_PICKS)):
        self.get, self.clock, self.picks, self.teams = get, clock, picks, {}

    def __call__(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        t = self.clock()
        picks, out = self.picks(), {}
        for slot in SLOTS:
            pick = picks.get(slot)
            if pick is None:   # the bus stop switched off: no card, and the 3D stop isn't drawn
                self.teams.pop(slot, None)
                continue
            e = self.teams.get(slot)
            if e is None or e["pick"] != pick:
                e = self.teams[slot] = {"pick": pick, "data": None, "good_at": None, "tried": None, "err": None}
            data = e["data"]
            wait = LIVE_REFRESH_S if data and data["live"] else REFRESH_S
            if e["tried"] is None or t - e["tried"] >= wait - 5:
                e["tried"] = t
                try:
                    e["data"], e["good_at"], e["err"] = _fetch(pick, self.get, now), t, None
                    e["data"]["fetched_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception as ex:  # noqa: BLE001 (only the class name is kept)
                    e["err"] = type(ex).__name__
                    if e["good_at"] is None or t - e["good_at"] > KEEP_S:
                        e["data"] = None
            out[slot] = {**(e["data"] or {"error": e["err"] or "unavailable"}), "team": team_meta(slot, pick)}
        return out
