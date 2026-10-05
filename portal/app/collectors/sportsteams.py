"""The leagues and teams the three stadium cards can show, and the small picks file the portal writes for the collector.
A pick is "league:key" (the same abbreviation exists in several leagues, so the league is part of the name): "mlb:111",
"nba:BOS", "nhl:BOS", "nfl:CAR", "mls:CLT". Only the portal's Settings page chooses the teams; the collector re-validates the
file (see read_picks), so a missing or odd file just means the defaults: Red Sox ballpark, Celtics arena, Hornets bus stop."""
import json
from pathlib import Path
from typing import NamedTuple

from . import teamdata

SLOTS = ("ballpark", "arena", "busstop")
DEFAULT_PICKS = {"ballpark": "mlb:111", "arena": "nba:BOS", "busstop": "nba:CHA"}
INVALID = object()   # parse_form's answer for a value that isn't a choice

W, L, GB, OTL, T, D, PTS = ("w", "W"), ("l", "L"), ("gb", "GB"), ("otl", "OTL"), ("t", "T"), ("t", "D"), ("pts", "PTS")
EAST_WEST = {"East": "Eastern Conference", "West": "Western Conference"}


class League(NamedTuple):
    label: str        # menu heading, e.g. "Basketball (NBA)"
    sport: str        # what a card says: "Basketball"
    path: str         # ESPN's feed path ("" for MLB, which has its own API)
    teams: dict       # key -> (name, nickname, city, group title, colour, dark colour, ESPN id)
    groups: dict      # group title -> the conference name in ESPN's standings feed
    cols: tuple       # standings columns: ((row key, heading), ...)
    season: str       # how ESPN numbers a season: "end" (the year it ends in) or "calendar" (the year it starts in)
    min_recent: int   # fewer finished games than this early in a season: also pull in last season's tail


def _mlb_teams() -> dict:
    out = {}
    for tid, (name, short, division) in teamdata.MLB.items():
        city = name[: -len(short)].strip() if name.endswith(short) and name != short else name
        out[str(tid)] = (name, short, city, division, "#BD3039", "#0C2340", "")   # the ballpark doesn't recolour
    return out


LEAGUES = {
    "mlb": League("Baseball (MLB)", "Baseball", "", _mlb_teams(), {}, (W, L, GB), "calendar", 0),
    "nba": League("Basketball (NBA)", "Basketball", "basketball/nba", teamdata.NBA, EAST_WEST, (W, L, GB), "end", 5),
    "wnba": League("Basketball (WNBA)", "Basketball", "basketball/wnba", teamdata.WNBA, EAST_WEST, (W, L, GB), "calendar", 5),
    "nhl": League("Hockey (NHL)", "Hockey", "hockey/nhl", teamdata.NHL, EAST_WEST, (W, L, OTL, PTS), "end", 5),
    "nfl": League("Football (NFL)", "Football", "football/nfl", teamdata.NFL,
                  {"AFC": "American Football Conference", "NFC": "National Football Conference"}, (W, L, T), "calendar", 3),
    "mls": League("Soccer (MLS)", "Soccer", "soccer/usa.1", teamdata.MLS, EAST_WEST, (W, D, L, PTS), "calendar", 5),
}
SLOT_LEAGUES = {"ballpark": ("mlb",), "arena": ("nba", "wnba", "nhl"), "busstop": ("nba", "wnba", "nhl", "nfl", "mls")}


def split_pick(pick) -> tuple:
    league, _, key = str(pick).partition(":")
    return league, key


def valid_pick(slot, value) -> bool:
    if slot not in SLOT_LEAGUES or not isinstance(value, str):
        return False
    league, key = split_pick(value)
    return league in SLOT_LEAGUES[slot] and key in LEAGUES[league].teams


def clean_picks(doc) -> dict:
    """Any document -> one valid pick per slot. A bad or missing slot takes its default; only the bus stop may be None (off)."""
    out = dict(DEFAULT_PICKS)
    if not isinstance(doc, dict):
        return out
    for slot in SLOTS:
        if slot not in doc:
            continue
        v = doc[slot]
        if slot == "busstop" and v is None:
            out[slot] = None
        elif valid_pick(slot, v):
            out[slot] = v
    return out


def read_picks(path) -> dict:
    """The portal-written picks file, else the defaults. Anything odd means the default for that slot."""
    if path is None:
        return dict(DEFAULT_PICKS)
    try:
        return clean_picks(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return dict(DEFAULT_PICKS)


def write_picks(path, picks: dict) -> None:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(clean_picks(picks)), encoding="utf-8")
    tmp.replace(p)


def parse_form(slot, raw):
    """A Settings form value -> a valid pick, None (bus stop off), or INVALID. Free text never gets further than this."""
    raw = str(raw or "").strip()
    if slot == "busstop" and raw.lower() in ("", "none"):
        return None
    return raw if valid_pick(slot, raw) else INVALID


def menu(slot) -> list:
    """The Settings dropdown for a slot: [(group heading, [(value, team name), ...]), ...], teams sorted by name."""
    return [(LEAGUES[lg].label, sorted(((f"{lg}:{k}", t[0]) for k, t in LEAGUES[lg].teams.items()), key=lambda x: x[1]))
            for lg in SLOT_LEAGUES[slot]]


def team_meta(slot, pick) -> dict:
    """What a card needs to name and colour a team, from our own tables (nothing here comes from a feed)."""
    league, key = split_pick(pick)
    L = LEAGUES[league]
    name, short, city, title, color, color2, espn_id = L.teams[key]
    return {"abbr": key, "league": league, "sport": L.sport, "name": name, "short": short, "city": city,
            "color": color, "color2": color2, "title": title}
