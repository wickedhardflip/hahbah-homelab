import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import settingsstore
from app.collectors import sportsteams as st
from app.main import create_app
from app.models import User
from conftest import FakeBuilder, make_user, sign_in

HEX = set("0123456789abcdefABCDEF")


# ---------- the tables ----------

def test_every_league_has_all_its_teams():
    assert {k: len(v.teams) for k, v in st.LEAGUES.items()} == {"mlb": 30, "nba": 30, "wnba": 15, "nhl": 32, "nfl": 32, "mls": 30}


def test_every_team_row_is_well_formed():
    for lg, L in st.LEAGUES.items():
        for key, (name, short, city, title, c1, c2, espn_id) in L.teams.items():
            assert name and short and city, (lg, key)
            assert len(c1) == len(c2) == 7 and c1[0] == c2[0] == "#" and set(c1[1:] + c2[1:]) <= HEX, (lg, key)
            assert (title in L.groups) if L.groups else title.startswith(("AL ", "NL ")), (lg, key, title)
            assert espn_id.isdigit() if lg == "mls" else espn_id == "", (lg, key)


def test_leagues_know_their_standings_columns():
    assert [c[1] for c in st.LEAGUES["nba"].cols] == ["W", "L", "GB"]
    assert [c[1] for c in st.LEAGUES["nhl"].cols] == ["W", "L", "OTL", "PTS"]
    assert [c[1] for c in st.LEAGUES["nfl"].cols] == ["W", "L", "T"]
    assert [c[1] for c in st.LEAGUES["mls"].cols] == ["W", "D", "L", "PTS"]


def test_defaults_are_the_current_three_teams():
    assert st.DEFAULT_PICKS == {"ballpark": "mlb:111", "arena": "nba:BOS", "busstop": "nba:CHA"}


# ---------- which league fits which slot ----------

@pytest.mark.parametrize("slot,pick,ok", [
    ("ballpark", "mlb:147", True), ("ballpark", "nba:BOS", False), ("ballpark", "mlb:999", False),
    ("arena", "nba:LAL", True), ("arena", "nhl:BOS", True), ("arena", "wnba:MIN", True),
    ("arena", "nfl:CAR", False), ("arena", "mls:CLT", False), ("arena", "mlb:111", False),
    ("busstop", "nfl:CAR", True), ("busstop", "mls:CLT", True), ("busstop", "nhl:MTL", True), ("busstop", "nba:CHA", True),
    ("busstop", "mlb:111", False),
    ("arena", "BOS", False), ("arena", "nba:", False), ("arena", ":BOS", False), ("arena", "xyz:BOS", False),
    ("arena", "nba:bos", False), ("arena", 5, False), ("arena", None, False), ("nope", "nba:BOS", False),
    ("ballpark", 111, False),
])
def test_valid_pick(slot, pick, ok):
    assert st.valid_pick(slot, pick) is ok


def test_the_same_abbreviation_in_two_leagues_is_two_teams():
    assert st.team_meta("arena", "nba:BOS")["name"] == "Boston Celtics"
    assert st.team_meta("arena", "nhl:BOS")["name"] == "Boston Bruins"


# ---------- clean / read / write ----------

def test_clean_picks_accepts_valid_and_falls_back_per_slot():
    good = {"ballpark": "mlb:147", "arena": "nhl:BOS", "busstop": "nfl:CAR"}
    assert st.clean_picks(good) == good
    assert st.clean_picks({**good, "ballpark": "mlb:999"}) == {**good, "ballpark": "mlb:111"}
    assert st.clean_picks({**good, "arena": "nfl:CAR"}) == {**good, "arena": "nba:BOS"}    # football can't be the arena
    assert st.clean_picks({"ballpark": 147, "arena": "nope", "busstop": 5}) == st.DEFAULT_PICKS


def test_busstop_can_be_switched_off_but_the_others_cannot():
    assert st.clean_picks({"busstop": None})["busstop"] is None
    assert st.clean_picks({"arena": None})["arena"] == "nba:BOS"
    assert st.clean_picks({"ballpark": None})["ballpark"] == "mlb:111"


def test_clean_picks_survives_garbage():
    for junk in (None, [], "x", 5, {"ballpark": [1], "arena": {"a": 1}}):
        assert st.clean_picks(junk) == st.DEFAULT_PICKS


def test_write_then_read_round_trips(tmp_path):
    p = tmp_path / "sports.json"
    picks = {"ballpark": "mlb:147", "arena": "nba:GS", "busstop": None}
    st.write_picks(p, picks)
    assert json.loads(p.read_text(encoding="utf-8")) == picks
    assert st.read_picks(p) == picks
    assert not list(tmp_path.glob("*.tmp"))


def test_read_picks_missing_or_broken_file_means_defaults(tmp_path):
    assert st.read_picks(tmp_path / "nope.json") == st.DEFAULT_PICKS
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert st.read_picks(tmp_path / "bad.json") == st.DEFAULT_PICKS
    assert st.read_picks(None) == st.DEFAULT_PICKS


# ---------- team_meta, form parsing, menus ----------

def test_team_meta_carries_what_the_dashboard_needs():
    m = st.team_meta("arena", "nba:BOS")
    assert (m["name"], m["short"], m["city"], m["abbr"], m["league"], m["sport"]) == ("Boston Celtics", "Celtics", "Boston", "BOS", "nba", "Basketball")
    assert m["color"].startswith("#") and m["color2"].startswith("#")
    b = st.team_meta("ballpark", "mlb:111")
    assert (b["name"], b["short"], b["sport"], b["city"], b["title"]) == ("Boston Red Sox", "Red Sox", "Baseball", "Boston", "AL East")
    assert st.team_meta("busstop", "nba:CHA")["city"] == "Charlotte"
    assert st.team_meta("busstop", "nfl:CAR")["sport"] == "Football" and st.team_meta("busstop", "mls:CLT")["sport"] == "Soccer"
    assert st.team_meta("arena", "nhl:BOS")["sport"] == "Hockey"


def test_form_value_parsing():
    assert st.parse_form("ballpark", "mlb:147") == "mlb:147"
    assert st.parse_form("arena", "nhl:BOS") == "nhl:BOS"
    assert st.parse_form("busstop", "") is None and st.parse_form("busstop", "none") is None
    for slot, bad in (("ballpark", "147"), ("ballpark", "mlb:999"), ("arena", "nfl:CAR"), ("arena", ""), ("arena", "none"),
                      ("busstop", "ZZZ"), ("busstop", "nba:ZZZ"), ("nope", "nba:BOS")):
        assert st.parse_form(slot, bad) is st.INVALID


def test_menus_group_teams_by_league_per_slot():
    assert [h for h, _ in st.menu("ballpark")] == ["Baseball (MLB)"]
    assert [h for h, _ in st.menu("arena")] == ["Basketball (NBA)", "Basketball (WNBA)", "Hockey (NHL)"]
    assert [h for h, _ in st.menu("busstop")] == ["Basketball (NBA)", "Basketball (WNBA)", "Hockey (NHL)", "Football (NFL)", "Soccer (MLS)"]
    nhl = dict(st.menu("arena"))["Hockey (NHL)"]
    assert ("nhl:BOS", "Boston Bruins") in nhl and [n for _, n in nhl] == sorted(n for _, n in nhl) and len(nhl) == 32
    assert all(st.valid_pick("busstop", v) for _, teams in st.menu("busstop") for v, _ in teams)


# ---------- Settings page ----------

SNAP = {"schema": "homelab.snapshot/v1", "nodes": [], "links": [], "alerts": [], "layout": {"lines": [], "pos": {}, "via": {}},
        "edge": {}, "sources": []}
FORM = {"action": "sports", "ballpark": "mlb:147", "arena": "nhl:BOS", "busstop": "nfl:CAR"}


@pytest.fixture
def papp(settings, tmp_path):
    return create_app(replace(settings, sports_file=tmp_path / "sports.json"), builder=FakeBuilder(SNAP), collect=False)


def client(app, admin=True):
    make_user(app)
    with app.state.SessionLocal() as db:
        db.query(User).one().is_admin = admin
        db.commit()
    c = TestClient(app)
    sign_in(c)
    return c


def test_settings_page_offers_the_right_teams_and_marks_the_current_ones(papp):
    page = client(papp).get("/settings").text
    for name in ("Boston Red Sox", "New York Yankees", "Boston Celtics", "Boston Bruins", "Minnesota Lynx", "Carolina Panthers",
                 "Charlotte FC", "None (hide it)"):
        assert name in page
    assert '<option value="mlb:111" selected>' in page and '<option value="nba:BOS" selected>' in page and '<option value="nba:CHA" selected>' in page
    arena = page.split('name="arena"')[1].split("</select>")[0]
    assert "Carolina Panthers" not in arena and "Charlotte FC" not in arena and "Boston Bruins" in arena   # football and soccer aren't arenas
    assert "Carolina Panthers" in page.split('name="busstop"')[1].split("</select>")[0]


def test_saving_teams_stores_them_and_writes_the_file(papp, tmp_path):
    r = client(papp).post("/settings", data=FORM, follow_redirects=True)
    assert "Teams saved" in r.text and '<option value="mlb:147" selected>' in r.text and '<option value="nfl:CAR" selected>' in r.text
    assert json.loads((tmp_path / "sports.json").read_text()) == {"ballpark": "mlb:147", "arena": "nhl:BOS", "busstop": "nfl:CAR"}
    with papp.state.SessionLocal() as db:
        assert settingsstore.get(db, "sports_ballpark") == "mlb:147" and settingsstore.get(db, "sports_arena") == "nhl:BOS"


def test_bus_stop_can_be_hidden(papp, tmp_path):
    r = client(papp).post("/settings", data={**FORM, "busstop": "none"}, follow_redirects=True)
    assert '<option value="none" selected>' in r.text
    assert json.loads((tmp_path / "sports.json").read_text())["busstop"] is None


@pytest.mark.parametrize("form", [
    {"ballpark": "999"}, {"ballpark": "147"}, {"ballpark": "nba:BOS"}, {"ballpark": ""},
    {"arena": "<script>"}, {"arena": ""}, {"arena": "nfl:CAR"}, {"arena": "mls:CLT"}, {"arena": "BOS"},
    {"busstop": "../etc"}, {"busstop": "nba:ZZZ"}, {"busstop": "mlb:111"},
])
def test_a_value_off_the_lists_changes_nothing(papp, tmp_path, form):
    r = client(papp).post("/settings", data={**FORM, **form}, follow_redirects=True)
    assert "isn&#39;t on the list" in r.text or "isn't on the list" in r.text
    assert not (tmp_path / "sports.json").exists()
    with papp.state.SessionLocal() as db:
        assert settingsstore.get(db, "sports_arena") == "nba:BOS" and settingsstore.get(db, "sports_ballpark") == "mlb:111"


def test_sports_setting_is_admin_only(papp):
    assert client(papp, admin=False).post("/settings", data=FORM).status_code == 403


def test_sports_setting_refuses_cross_site_posts(papp):
    c = client(papp)
    assert c.post("/settings", data=FORM, headers={"Origin": "https://evil.example"}).status_code == 403


def test_saving_without_a_picks_file_configured_still_stores_the_choice(settings):
    app = create_app(settings, builder=FakeBuilder(SNAP), collect=False)
    client(app).post("/settings", data=FORM)
    with app.state.SessionLocal() as db:
        assert settingsstore.get(db, "sports_ballpark") == "mlb:147"


def test_sidecar_job_reads_its_teams_from_the_picks_file(tmp_path):
    from app.collectors import sports
    from app.sidecar import build_jobs
    f = tmp_path / "sports.json"
    st.write_picks(f, {"ballpark": "mlb:147", "arena": "nhl:BOS", "busstop": None})
    env = {"SPORTS": "1", "SPORTS_PICKS": str(f)}
    job = next(j for j in build_jobs(lambda k, d=None: env.get(k, d)) if j.name == "sports")
    seen = []
    job.fn.get = lambda url: (seen.append(url), (_ for _ in ()).throw(OSError("x")))[1]
    out = job.fn(sports.datetime(2026, 10, 2, 15, 0, tzinfo=sports.timezone.utc))
    assert set(out) == {"ballpark", "arena"} and any("teamId=147" in u for u in seen) and any("/hockey/nhl/teams/bos/" in u for u in seen)
