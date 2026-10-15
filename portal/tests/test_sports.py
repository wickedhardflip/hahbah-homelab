import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from app.collectors import sports

FX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)


def fx(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def test_mlb_results_and_home_away():
    games = sports.parse_mlb_games(fx("mlb_schedule.json"))
    assert len(games) == 9 and all(g["state"] == "post" for g in games)
    g = games[0]   # 9/20: Red Sox away at Tampa, lost 1-5
    assert (g["opp"], g["home"], g["us"], g["them"], g["result"]) == ("Tampa Bay Rays", False, 1, 5, "L")


def test_mlb_live_pre_and_postponed():
    doc = fx("mlb_schedule.json")
    games = doc["dates"][-1]["games"]
    g = copy.deepcopy(games[0])
    g["status"] = {"abstractGameState": "Live", "detailedState": "In Progress"}
    g["linescore"] = {"currentInningOrdinal": "7th", "inningHalf": "Bottom"}
    p = copy.deepcopy(games[0])
    p["status"] = {"abstractGameState": "Preview", "detailedState": "Scheduled"}
    p["gameDate"] = "2026-10-03T23:10:00Z"
    p["teams"]["home"].pop("score"), p["teams"]["away"].pop("score")
    pp = copy.deepcopy(games[0])
    pp["status"] = {"abstractGameState": "Final", "detailedState": "Postponed"}
    games += [g, p, pp]
    last, nxt, live = sports._pack(sports.parse_mlb_games(doc))
    assert len(last) == 5 and last[-1]["date"] < "2026-10-02"
    assert live["detail"] == "Bottom 7th" and live["us"] is not None
    assert nxt["date"] == "2026-10-03T23:10:00Z"


def test_mlb_standings_al_east():
    s = sports.parse_mlb_standings(fx("mlb_standings.json"))
    assert s["title"] == "AL East" and len(s["rows"]) == 5
    assert sum(r["us"] for r in s["rows"]) == 1
    assert sports.parse_mlb_standings(fx("mlb_standings.json"), team_id=999) is None


def test_espn_past_and_preseason():
    past = sports.parse_espn_games(fx("espn_schedule_past.json"))
    assert len(past) == 6 and all(g["state"] == "post" and g["result"] in "WL" for g in past)
    assert past[-1]["home"] and past[-1]["opp"] == "Orlando Magic" and (past[-1]["us"], past[-1]["them"]) == (113, 108)
    pre = sports.parse_espn_games(fx("espn_schedule_pre.json"))
    last, nxt, live = sports._pack(pre)
    assert last == [] and live is None and nxt["opp"] and nxt["date"].startswith("2026-10")


def test_espn_standings_east_only():
    s = sports.parse_espn_standings(fx("espn_standings.json"))
    assert s["title"] == "East" and len(s["rows"]) == 15 and sum(r["us"] for r in s["rows"]) == 1
    assert set(s["rows"][0]) == {"name", "w", "l", "gb", "us", "logo"}


def fake_get(fail=()):
    def get(url):
        if any(f in url for f in fail):
            raise OSError("down")
        if "statsapi" in url and "standings" in url:
            return fx("mlb_standings.json")
        if "statsapi" in url:
            return fx("mlb_schedule.json")
        if "standings" in url:
            return fx("espn_standings.json")
        if "/teams/cha/" in url:
            return fx("espn_schedule_cha.json")
        if "seasontype=2" in url:
            return fx("espn_schedule_past.json")
        if "seasontype=3" in url:
            return {"events": []}
        return fx("espn_schedule_pre.json")
    return get


def test_job_builds_both_cards():
    out = sports.Sports(fake_get(), clock=lambda: 0.0)(NOW)
    assert len(out["ballpark"]["last"]) == 5 and out["ballpark"]["standings"]
    # Celtics: preseason schedule + last season's tail
    assert len(out["arena"]["last"]) == 5 and out["arena"]["next"] and out["arena"]["fetched_at"]


def test_one_source_failing_leaves_the_other():
    out = sports.Sports(fake_get(fail=("espn.com",)), clock=lambda: 0.0)(NOW)
    assert out["arena"]["error"] == "OSError" and out["ballpark"]["last"]


def test_standings_failure_keeps_the_rest():
    out = sports.Sports(fake_get(fail=("standings",)), clock=lambda: 0.0)(NOW)
    assert out["ballpark"]["standings"] is None and out["ballpark"]["last"] and out["arena"]["standings"] is None


def test_refresh_cadence_and_keep_last_good():
    t = [0.0]
    calls = []
    inner = fake_get()
    def get(url):
        calls.append(url)
        if t[0] > 1000 and "espn.com" in url:
            raise OSError("down")
        return inner(url)
    s = sports.Sports(get, clock=lambda: t[0])
    s(NOW)
    n = len(calls)
    t[0] = 120
    s(NOW)
    assert len(calls) == n            # not live: no refetch inside 15 minutes
    t[0] = 1000
    s(NOW)
    assert len(calls) > n             # 15 minutes later: refetched
    t[0] = 1900
    out = s(NOW)                      # ESPN down but our copy is under an hour old
    assert out["arena"].get("last")
    t[0] = 1000 + 3700
    s(NOW)
    assert s(NOW)["arena"]["error"] == "OSError"


def test_live_game_refreshes_every_two_minutes():
    t = [0.0]
    calls = []
    inner = fake_get()
    def get(url):
        calls.append(url)
        d = copy.deepcopy(inner(url))
        if "statsapi" in url and "standings" not in url:
            g = d["dates"][-1]["games"][0]
            g["status"] = {"abstractGameState": "Live", "detailedState": "In Progress"}
        return d
    s = sports.Sports(get, clock=lambda: t[0])
    assert s(NOW)["ballpark"]["live"]
    n = len(calls)
    t[0] = 125
    s(NOW)
    assert len(calls) > n


def test_sports_visible_to_non_admins():
    from app.views import snapshot_for
    snap = {"nodes": [], "sports": {"ballpark": {"last": [], "next": None, "live": None, "standings": None}}}
    assert snapshot_for(snap, False)["sports"] == snap["sports"]


def test_sidecar_registers_the_job():
    from app.sidecar import build_jobs
    assert "sports" in [j.name for j in build_jobs(lambda k, d=None: {"SPORTS": "1"}.get(k, d))]
    assert "sports" not in [j.name for j in build_jobs(lambda k, d=None: {"SPORTS": "0"}.get(k, d))]


def test_mlb_skips_college_exhibitions():
    doc = fx("mlb_schedule.json")
    doc["dates"][0]["games"][0]["gameType"] = "E"
    assert len(sports.parse_mlb_games(doc)) == 8


def test_logos_for_both_teams_come_from_the_two_allowed_hosts():
    mlb = sports.parse_mlb_games(fx("mlb_schedule.json"))
    assert all(g["opp_logo"].startswith("https://www.mlbstatic.com/team-logos/") and g["opp_logo"].endswith(".svg") for g in mlb)
    red = sports.fetch_mlb(lambda url: fx("mlb_schedule.json") if "schedule" in url else fx("mlb_standings.json"), NOW, 111)
    assert red["logo"] == "https://www.mlbstatic.com/team-logos/111.svg" and red["last"][0]["opp_logo"]
    assert all(r["logo"].startswith("https://www.mlbstatic.com/") for r in sports.parse_mlb_standings(fx("mlb_standings.json"))["rows"])
    espn = sports.parse_espn_games(fx("espn_schedule_past.json"))
    assert all(g["opp_logo"].startswith("https://a.espncdn.com/") for g in espn)
    assert all(r["logo"].startswith("https://a.espncdn.com/") for r in sports.parse_espn_standings(fx("espn_standings.json"))["rows"])


def test_espn_logo_prefers_the_feed_but_never_an_unknown_host():
    assert sports.espn_logo({"logo": "https://a.espncdn.com/x/y.png"}) == "https://a.espncdn.com/x/y.png"
    assert sports.espn_logo({"logo": "https://evil.example/x.png", "abbreviation": "BOS"}) == "https://a.espncdn.com/i/teamlogos/nba/500/bos.png"
    assert sports.espn_logo({"logo": "http://a.espncdn.com/x.png"}) is None
    assert sports.mlb_logo(None) is None


def test_hornets_card_matches_the_celtics_shape():
    out = sports.Sports(fake_get(), clock=lambda: 0.0)(NOW)
    h = out["busstop"]
    assert set(h) == set(out["arena"]) and h["team"]["short"] == "Hornets"
    assert len(h["last"]) == 5 and [g["result"] for g in h["last"]] == ["W", "L", "W", "T", "W"]
    assert h["last"][0]["home"] is True and h["last"][1]["home"] is False   # Hornets are read as "us", not Boston
    assert h["live"] is None and h["next"]["opp"] == "Boston Celtics" and h["next"]["home"] is True
    assert h["standings"]["title"] == "East" and [r["name"] for r in h["standings"]["rows"] if r["us"]] == ["Hornets"]
    assert h["logo"] == "https://a.espncdn.com/i/teamlogos/nba/500/cha.png" and h["fetched_at"]
    assert all(g["opp_logo"].startswith("https://a.espncdn.com/") for g in h["last"])


def test_hornets_failure_leaves_celtics_and_red_sox():
    out = sports.Sports(fake_get(fail=("/teams/cha/",)), clock=lambda: 0.0)(NOW)
    assert out["busstop"]["error"] == "OSError"
    assert out["arena"]["last"] and out["ballpark"]["last"]


def test_hornets_live_game_is_read_from_the_hornets_side():
    doc = fx("espn_schedule_cha.json")
    c = doc["events"][5]["competitions"][0]
    c["status"]["type"].update(name="STATUS_IN_PROGRESS", state="in", shortDetail="Q3 4:12")
    for x, v in zip(c["competitors"], (60, 58)):
        x["score"] = {"value": float(v), "displayValue": str(v)}
    last, nxt, live = sports._pack(sports.parse_espn_games(doc, "CHA"))
    assert live["opp"] == "Boston Celtics" and (live["us"], live["them"]) == (60, 58) and live["detail"] == "Q3 4:12"
    assert nxt["opp"] == "Cleveland Cavaliers" and nxt["home"] is False


# ---------- configurable teams ----------

def urls_for(picks, fail=()):
    calls = []
    inner = fake_get(fail)
    def get(url):
        calls.append(url)
        return inner(url)
    out = sports.Sports(get, clock=lambda: 0.0, picks=lambda: picks)(NOW)
    return out, calls


def test_default_picks_name_the_three_current_teams():
    out = sports.Sports(fake_get(), clock=lambda: 0.0)(NOW)
    assert [out[k]["team"]["short"] for k in ("ballpark", "arena", "busstop")] == ["Red Sox", "Celtics", "Hornets"]
    assert out["ballpark"]["team"]["sport"] == "Baseball" and out["arena"]["team"]["city"] == "Boston"


def test_each_slot_fetches_the_team_it_is_set_to():
    out, calls = urls_for({"ballpark": "mlb:147", "arena": "nba:LAL", "busstop": "nba:MIA"})
    joined = " ".join(calls)
    assert "teamId=147" in joined and "teamId=111" not in joined
    assert "/teams/lal/" in joined and "/teams/mia/" in joined and "/teams/bos/" not in joined and "/teams/cha/" not in joined
    assert [out[k]["team"]["short"] for k in ("ballpark", "arena", "busstop")] == ["Yankees", "Lakers", "Heat"]
    assert out["ballpark"]["logo"] == "https://www.mlbstatic.com/team-logos/147.svg"
    assert out["arena"]["logo"].startswith("https://a.espncdn.com/")   # (the fake feed serves one schedule for every team)


def test_bus_stop_off_means_no_busstop_card_and_no_fetch():
    out, calls = urls_for({"ballpark": "mlb:111", "arena": "nba:BOS", "busstop": None})
    assert "busstop" not in out and "ballpark" in out and "arena" in out
    assert not any("/teams/cha/" in c for c in calls)


def test_a_failed_slot_still_names_its_team():
    out, _ = urls_for({"ballpark": "mlb:111", "arena": "nba:BOS", "busstop": "nba:CHA"}, fail=("/teams/cha/",))
    assert out["busstop"]["error"] == "OSError" and out["busstop"]["team"]["short"] == "Hornets"


def test_changing_a_pick_starts_that_slot_over():
    picks = {"ballpark": "mlb:111", "arena": "nba:BOS", "busstop": "nba:CHA"}
    calls = []
    inner = fake_get()
    def get(url):
        calls.append(url)
        return inner(url)
    s = sports.Sports(get, clock=lambda: 0.0, picks=lambda: picks)
    s(NOW)
    n = len(calls)
    s(NOW)
    assert len(calls) == n                              # unchanged picks, inside 15 minutes: nothing refetched
    picks["arena"] = "nba:GS"
    out = s(NOW)
    assert len(calls) > n and any("/teams/gs/" in c for c in calls[n:])
    assert out["arena"]["team"]["short"] == "Warriors" and out["ballpark"]["team"]["short"] == "Red Sox"
    assert not any("teamId" in c for c in calls[n:])    # the ballpark was not refetched


def test_old_data_never_leaks_into_a_newly_picked_team():
    picks = {"ballpark": "mlb:111", "arena": "nba:BOS", "busstop": "nba:CHA"}
    t = [0.0]
    inner = fake_get()
    def get(url):
        if t[0] > 0 and "/teams/gs/" in url:
            raise OSError("down")
        return inner(url)
    s = sports.Sports(get, clock=lambda: t[0], picks=lambda: picks)
    assert s(NOW)["arena"]["last"]
    t[0] = 10
    picks["arena"] = "nba:GS"
    out = s(NOW)["arena"]
    assert out["error"] == "OSError" and "last" not in out and out["team"]["short"] == "Warriors"   # not the Celtics' games


def test_mlb_standings_follow_the_team_into_its_division():
    doc = fx("mlb_standings.json")
    other = copy.deepcopy(doc["records"][0])
    for r in other["teamRecords"]:
        r["team"]["id"] += 1000
    other["teamRecords"][0]["team"]["id"] = 121   # the Mets, who are not in the fixture's AL East
    doc["records"].append(other)
    s = sports.parse_mlb_standings(doc, 121, "NL East")
    assert s["title"] == "NL East" and sum(r["us"] for r in s["rows"]) == 1 and s["rows"][0]["us"]


def test_espn_standings_pick_the_teams_conference():
    doc = fx("espn_standings.json")
    west = copy.deepcopy(next(c for c in doc["children"] if c["name"] == "Eastern Conference"))
    west["name"] = "Western Conference"
    west["standings"]["entries"][0]["team"]["abbreviation"] = "LAL"
    doc["children"].append(west)
    s = sports.parse_espn_standings(doc, "LAL", "West")
    assert s["title"] == "West" and sum(r["us"] for r in s["rows"]) == 1
    assert sports.parse_espn_standings({"children": [west]}, "LAL", "East") is None


# ---------- other leagues ----------

def other_get(url):
    """A feed that answers for hockey, football and soccer with the same fixture shapes (one schedule for every team)."""
    if "standings" in url:
        return fx("espn_standings.json")
    return fx("espn_schedule_past.json")


def test_hockey_arena_and_football_bus_stop_end_to_end():
    calls = []
    def get(url):
        calls.append(url)
        return other_get(url) if "espn.com" in url else fake_get()(url)
    picks = {"ballpark": "mlb:111", "arena": "nhl:BOS", "busstop": "nfl:CAR"}
    out = sports.Sports(get, clock=lambda: 0.0, picks=lambda: picks)(NOW)
    joined = " ".join(calls)
    assert "/hockey/nhl/teams/bos/schedule" in joined and "/football/nfl/teams/car/schedule" in joined
    assert "/apis/v2/sports/hockey/nhl/standings" in joined and "/apis/v2/sports/football/nfl/standings" in joined
    assert out["arena"]["team"]["name"] == "Boston Bruins" and out["arena"]["team"]["sport"] == "Hockey"
    assert out["busstop"]["team"]["name"] == "Carolina Panthers" and out["busstop"]["team"]["sport"] == "Football"
    assert out["arena"]["last"] and out["busstop"]["last"]


def test_mls_is_addressed_by_its_espn_id_and_logo():
    calls = []
    def get(url):
        calls.append(url)
        return other_get(url)
    out = sports.fetch_espn(get, NOW, "mls", "CLT")
    assert any("/soccer/usa.1/teams/21300/schedule" in c for c in calls)
    assert out["logo"].startswith("https://a.espncdn.com/")
    assert sports.espn_logo({"id": "21300"}, "mls") == "https://a.espncdn.com/i/teamlogos/soccer/500/21300.png"
    assert sports.espn_logo({"abbreviation": "BOS"}, "nhl") == "https://a.espncdn.com/i/teamlogos/nhl/500/bos.png"
    assert sports.espn_logo({"abbreviation": "CAR"}, "nfl") == "https://a.espncdn.com/i/teamlogos/nfl/500/car.png"
    assert sports.espn_logo({"abbreviation": "MIN"}, "wnba") == "https://a.espncdn.com/i/teamlogos/wnba/500/min.png"


def test_wnba_uses_its_own_feed_path():
    calls = []
    def get(url):
        calls.append(url)
        return other_get(url)
    sports.fetch_espn(get, NOW, "wnba", "MIN")
    assert any("/basketball/wnba/teams/min/schedule" in c for c in calls)


def doc_with(group, team_abbr, stats):
    doc = copy.deepcopy(fx("espn_standings.json"))
    c = copy.deepcopy(doc["children"][0])
    c["name"] = group
    e = c["standings"]["entries"][0]
    e["team"]["abbreviation"] = team_abbr
    e["stats"] = [{"name": k, "displayValue": str(v)} for k, v in stats.items()]
    return {"children": [c]}, c


def test_hockey_standings_have_overtime_losses_and_points():
    doc, _ = doc_with("Eastern Conference", "BOS", {"wins": 40, "losses": 30, "otLosses": 12, "points": 92, "playoffSeed": 1})
    s = sports.parse_espn_standings(doc, "BOS", "East", "nhl")
    assert [c[1] for c in s["cols"]] == ["W", "L", "OTL", "PTS"]
    row = next(r for r in s["rows"] if r["us"])
    assert (row["w"], row["l"], row["otl"], row["pts"]) == (40, 30, 12, 92)


def test_football_standings_use_the_conference_and_ties():
    doc, _ = doc_with("National Football Conference", "CAR", {"wins": 9, "losses": 7, "ties": 1})
    s = sports.parse_espn_standings(doc, "CAR", "NFC", "nfl")
    assert s["title"] == "NFC" and [c[1] for c in s["cols"]] == ["W", "L", "T"]
    assert next(r for r in s["rows"] if r["us"])["t"] == 1


def test_soccer_standings_have_draws_and_points():
    doc, _ = doc_with("Eastern Conference", "CLT", {"wins": 12, "losses": 14, "ties": 8, "points": 44})
    s = sports.parse_espn_standings(doc, "CLT", "East", "mls")
    assert [c[1] for c in s["cols"]] == ["W", "D", "L", "PTS"]
    row = next(r for r in s["rows"] if r["us"])
    assert (row["w"], row["t"], row["l"], row["pts"]) == (12, 8, 14, 44)


def test_changing_a_pick_across_leagues_starts_the_slot_over():
    picks = {"ballpark": "mlb:111", "arena": "nba:BOS", "busstop": "nba:CHA"}
    s = sports.Sports(other_get, clock=lambda: 0.0, picks=lambda: picks)
    s(NOW)
    picks["arena"] = "nhl:BOS"
    out = s(NOW)
    assert out["arena"]["team"]["short"] == "Bruins" and out["arena"]["team"]["league"] == "nhl"
