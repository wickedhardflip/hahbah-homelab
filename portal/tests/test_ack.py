"""Slice 4: Acknowledge on cards (recorder semantics, the endpoints, the snapshot, the digest, the migration)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app import emails, settingsstore
from app.db import migrate
from app.models import Incident
from conftest import make_user, sign_in
from test_recorder import DOWN, SLOW, T0, env, mails, snap, tick  # noqa: F401  (env is a fixture)

ORIGIN = {"Origin": "https://home.hahbah.com"}
KEY = "app_down:plex"


def dangers(outbox):
    return [m for m in mails(outbox) if "Danger" in m["Subject"]]


def clears(outbox):
    return [m for m in mails(outbox) if "All clear" in m["Subject"]]


def open_incident(rec, clock, alert):
    """Two checks in a row open the incident."""
    tick(rec, clock, snap([alert]), 2)


# ---------- recorder semantics ----------
def test_ack_before_email_skips_the_danger_email_even_after_unmute(env):
    rec, SL, clock, outbox = env
    with SL() as db:
        settingsstore.put(db, "mute_until", (T0 + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    open_incident(rec, clock, DOWN)
    assert rec.ack(KEY, "wife") is True
    with SL() as db:
        inc = db.query(Incident).one()
        assert inc.emailed and inc.acked_by == "wife" and inc.acked_at is not None
    tick(rec, clock, snap([DOWN]), 10)      # mute over, still down: no flood
    assert dangers(outbox) == []


def test_ack_after_email_suppresses_the_all_clear(env):
    rec, SL, clock, outbox = env
    open_incident(rec, clock, DOWN)
    assert len(dangers(outbox)) == 1
    rec.ack(KEY, "alex")
    tick(rec, clock, snap(), 30)            # closes, then 10 quiet minutes
    with SL() as db:
        assert db.query(Incident).one().closed_at is not None
    assert clears(outbox) == []


def test_escalation_clears_the_ack_and_rearms_the_danger_email(env):
    rec, SL, clock, outbox = env
    slow_crit = {**SLOW, "severity": "crit"}
    open_incident(rec, clock, SLOW)
    rec.ack("slow_speed:isp", "alex")
    tick(rec, clock, snap([slow_crit]))
    with SL() as db:
        inc = db.query(Incident).one()
        assert inc.severity == "crit" and inc.acked_by is None and inc.acked_at is None and inc.emailed
    assert len(dangers(outbox)) == 1


def test_undo_rearms_a_skipped_open_danger_but_never_resends(env):
    rec, SL, clock, outbox = env
    with SL() as db:
        settingsstore.put(db, "mute_until", (T0 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    open_incident(rec, clock, DOWN)         # muted: not emailed
    rec.ack(KEY, "alex")
    assert rec.unack(KEY) is True
    with SL() as db:
        inc = db.query(Incident).one()
        assert inc.acked_by is None and not inc.emailed   # un-emailed open Danger: re-armed
        settingsstore.put(db, "mute_until", "")
    tick(rec, clock, snap([DOWN]))
    assert len(dangers(outbox)) == 1
    rec.ack(KEY, "alex")                   # already emailed: undo sends nothing new
    rec.unack(KEY)
    tick(rec, clock, snap([DOWN]), 3)
    assert len(dangers(outbox)) == 1


def test_undo_of_a_caution_keeps_escalation_working(env):
    rec, SL, clock, outbox = env
    open_incident(rec, clock, SLOW)
    rec.ack("slow_speed:isp", "alex")
    rec.unack("slow_speed:isp")
    tick(rec, clock, snap([{**SLOW, "severity": "crit"}]))
    assert len(dangers(outbox)) == 1


def test_ack_ends_when_the_incident_closes_and_a_later_one_is_separate(env):
    rec, SL, clock, outbox = env
    open_incident(rec, clock, DOWN)
    rec.ack(KEY, "alex")
    tick(rec, clock, snap(), 30)
    assert rec.ack(KEY, "alex") is False and rec.unack(KEY) is False and rec.open_acks() == {}
    open_incident(rec, clock, DOWN)
    assert rec.open_acks() == {KEY: {"acked_by": None, "acked_at": None}}
    assert len(dangers(outbox)) == 2


def test_unknown_key_is_refused(env):
    rec, SL, clock, outbox = env
    assert rec.ack("app_down:nope", "alex") is False and rec.unack("app_down:nope") is False


def test_digest_marks_acknowledged_incidents(env):
    rec, SL, clock, outbox = env
    open_incident(rec, clock, DOWN)
    rec.ack(KEY, "wife")
    incs = rec.recent_incidents(24)
    assert incs[0]["acked_by"] == "wife"
    raw = emails.digest(snap(), incs, "alex@example.com", clock["now"], "America/New_York").decode()
    assert "acknowledged by wife" in raw.lower()


# ---------- migration ----------
def test_old_incidents_table_gets_the_ack_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50), password_hash TEXT, "
                          "is_admin BOOLEAN NOT NULL DEFAULT 0, google_email VARCHAR(254), created_at DATETIME)"))
        conn.execute(text("CREATE TABLE incidents (id INTEGER PRIMARY KEY, key VARCHAR(160), kind VARCHAR(40), target VARCHAR(80), "
                          "severity VARCHAR(8), message TEXT, opened_at DATETIME, closed_at DATETIME, emailed BOOLEAN, "
                          "clear_sent BOOLEAN NOT NULL DEFAULT 1)"))
        conn.execute(text("INSERT INTO incidents (key, kind, target, severity, message, opened_at, emailed) "
                          "VALUES ('app_down:plex', 'app_down', 'plex', 'crit', 'x', '2026-10-01 00:00:00', 1)"))
    migrate(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("incidents")}
    assert {"acked_by", "acked_at", "ack_skipped"} <= cols
    with engine.begin() as conn:
        assert conn.execute(text("SELECT acked_by, ack_skipped FROM incidents")).one() == (None, 0)
    migrate(engine)   # idempotent


# ---------- endpoints + snapshot ----------
def app_with_incident(app, builder, target="plex", kind="app_down", discreet=False):
    builder.snapshot["alerts"] = [{"id": "a1", "severity": "crit", "target": target, "kind": kind, "message": "down"}]
    with app.state.SessionLocal() as db:
        db.add(Incident(key=f"{kind}:{target}", kind=kind, target=target, severity="crit", message="down",
                        opened_at=datetime.now(timezone.utc).replace(tzinfo=None), emailed=True))
        if discreet:
            settingsstore.put(db, "discreet", "1")
        db.commit()
    make_user(app, "wife")
    c = TestClient(app)
    sign_in(c, "wife")   # not an admin
    return c


def test_ack_needs_sign_in(client):
    assert client.post("/api/incidents/app_down:plex/ack").status_code == 401
    assert client.post("/api/incidents/app_down:plex/unack").status_code == 401


def test_any_user_can_ack_and_undo_and_the_snapshot_carries_it(app, builder):
    c = app_with_incident(app, builder)
    a = c.get("/api/snapshot").json()["alerts"][0]
    assert a["incident"] is True and a["acked_by"] is None
    r = c.post("/api/incidents/app_down%3Aplex/ack", headers=ORIGIN)
    assert r.status_code == 200 and r.json()["acked_by"] == "wife"
    a = c.get("/api/snapshot").json()["alerts"][0]
    assert a["acked_by"] == "wife" and a["acked_at"]
    assert c.post("/api/incidents/app_down%3Aplex/unack", headers=ORIGIN).status_code == 200
    assert c.get("/api/snapshot").json()["alerts"][0]["acked_by"] is None
    assert "acked_by" not in builder.snapshot["alerts"][0]   # the shared snapshot is never mutated


def test_dashboard_page_embeds_ack_info(app, builder):
    c = app_with_incident(app, builder)
    c.post("/api/incidents/app_down%3Aplex/ack", headers=ORIGIN)
    assert '"acked_by": "wife"' in c.get("/").text


def test_alert_without_an_incident_has_no_ack_button_data(app, builder):
    c = app_with_incident(app, builder)
    builder.snapshot["alerts"].append({"id": "a2", "severity": "warn", "target": "isp", "kind": "slow_speed", "message": "slow"})
    a2 = [a for a in c.get("/api/snapshot").json()["alerts"] if a["id"] == "a2"][0]
    assert a2["incident"] is False
    assert c.post("/api/incidents/slow_speed%3Aisp/ack", headers=ORIGIN).status_code == 404


def test_unknown_or_closed_key_is_404(app, builder):
    c = app_with_incident(app, builder)
    assert c.post("/api/incidents/app_down%3Anope/ack", headers=ORIGIN).status_code == 404
    with app.state.SessionLocal() as db:
        db.query(Incident).update({"closed_at": datetime.now(timezone.utc).replace(tzinfo=None)})
        db.commit()
    assert c.post("/api/incidents/app_down%3Aplex/ack", headers=ORIGIN).status_code == 404
    assert c.post("/api/incidents/app_down%3Aplex/unack", headers=ORIGIN).status_code == 404


def test_cross_origin_is_refused(app, builder):
    c = app_with_incident(app, builder)
    bad = {"Origin": "https://evil.example"}
    assert c.post("/api/incidents/app_down%3Aplex/ack", headers=bad).status_code == 403
    assert c.post("/api/incidents/app_down%3Aplex/unack", headers=bad).status_code == 403
    with app.state.SessionLocal() as db:
        assert db.query(Incident).one().acked_by is None


def test_discreet_mode_hides_job_scraper_incidents(app, builder):
    c = app_with_incident(app, builder, target="jobs", discreet=True)
    assert c.get("/api/snapshot").json()["alerts"] == []
    assert c.post("/api/incidents/app_down%3Ajobs/ack", headers=ORIGIN).status_code == 404
    assert c.post("/api/incidents/app_down%3Ajobs/unack", headers=ORIGIN).status_code == 404
    with app.state.SessionLocal() as db:
        assert db.query(Incident).one().acked_by is None


def test_a_danger_that_was_acked_at_the_same_moment_is_not_emailed(env):
    """The ack landed after the recorder read the row but before it claimed it: the claim must lose, and Undo must not resend."""
    rec, SL, clock, outbox = env
    with SL() as db:
        settingsstore.put(db, "mute_until", (T0 + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    open_incident(rec, clock, DOWN)
    with SL() as db:   # the half-written state a racing ack could leave: acked but not yet marked emailed
        db.query(Incident).update({"acked_by": "wife", "emailed": False})
        db.commit()
    tick(rec, clock, snap([DOWN]), 10)
    assert dangers(outbox) == []


def test_ack_after_the_recorder_already_emailed_does_not_mark_it_skipped(env):
    rec, SL, clock, outbox = env
    open_incident(rec, clock, DOWN)
    assert len(dangers(outbox)) == 1
    rec.ack(KEY, "alex")
    rec.unack(KEY)
    tick(rec, clock, snap([DOWN]), 5)
    assert len(dangers(outbox)) == 1
