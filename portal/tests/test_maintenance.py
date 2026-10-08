"""Slice C: maintenance windows (recorder semantics with mute + ack, the digest, the page and its endpoints)."""
from datetime import timedelta

from fastapi.testclient import TestClient

from app import emails, settingsstore
from app.models import Incident, MaintenanceWindow, User
from conftest import make_user, sign_in
from test_ack import KEY, clears, dangers, open_incident
from test_recorder import DOWN, SLOW, T0, env, mails, snap, tick  # noqa: F401  (env is a fixture)

ORIGIN = {"Origin": "https://home.hahbah.com"}
ISO = "%Y-%m-%dT%H:%M:%SZ"


def window(rec, target=KEY, minutes=10, start=None):
    start = start or rec.now()
    return rec.add_window(target, start, start + timedelta(minutes=minutes), "patching", "alex")


def mute(SL, until):
    with SL() as db:
        settingsstore.put(db, "mute_until", until.strftime(ISO) if until else "")


# ---------- recorder semantics ----------
def test_active_window_holds_the_danger_email_and_sends_it_after_the_window(env):
    rec, SL, clock, outbox = env
    window(rec, minutes=5)
    open_incident(rec, clock, DOWN)
    tick(rec, clock, snap([DOWN]), 4)            # still inside the 5 min window
    assert dangers(outbox) == []
    with SL() as db:
        assert db.query(Incident).one().emailed is False   # held, not skipped
    tick(rec, clock, snap([DOWN]), 6)            # window over, still down
    assert len(dangers(outbox)) == 1


def test_incident_inside_the_window_gets_no_email_and_no_all_clear(env):
    rec, SL, clock, outbox = env
    window(rec, minutes=60)
    open_incident(rec, clock, DOWN)
    tick(rec, clock, snap(), 30)                 # closes + 10 quiet minutes, all inside the window
    tick(rec, clock, snap(), 80)                 # and past the window's end
    assert mails(outbox) == []


def test_emailed_incident_closing_in_a_window_holds_its_all_clear_until_the_end(env):
    rec, SL, clock, outbox = env
    open_incident(rec, clock, DOWN)
    assert len(dangers(outbox)) == 1
    window(rec, minutes=30)
    tick(rec, clock, snap(), 30)                 # closed 10+ min ago, window still on
    assert clears(outbox) == []
    tick(rec, clock, snap(), 40)
    assert len(clears(outbox)) == 1


def test_star_covers_everything(env):
    rec, SL, clock, outbox = env
    window(rec, "*", minutes=30)
    open_incident(rec, clock, DOWN)
    tick(rec, clock, snap([DOWN]), 3)
    assert dangers(outbox) == []


def test_window_for_another_key_suppresses_nothing(env):
    rec, SL, clock, outbox = env
    window(rec, "app_down:sonarr", minutes=30)
    open_incident(rec, clock, DOWN)
    assert len(dangers(outbox)) == 1


def test_cancelled_window_suppresses_nothing(env):
    rec, SL, clock, outbox = env
    wid = window(rec, minutes=30)
    assert rec.cancel_window(wid) is True and rec.cancel_window(999) is False
    open_incident(rec, clock, DOWN)
    assert len(dangers(outbox)) == 1


def test_expired_and_future_windows_suppress_nothing(env):
    rec, SL, clock, outbox = env
    window(rec, minutes=10, start=T0 - timedelta(hours=1))
    window(rec, minutes=10, start=T0 + timedelta(hours=1))
    open_incident(rec, clock, DOWN)
    assert len(dangers(outbox)) == 1


def test_window_ending_while_muted_waits_for_the_mute(env):
    rec, SL, clock, outbox = env
    mute(SL, T0 + timedelta(minutes=20))
    window(rec, minutes=5)
    open_incident(rec, clock, DOWN)
    tick(rec, clock, snap([DOWN]), 20)           # window over at 5 min, mute still on until 20
    assert dangers(outbox) == []
    tick(rec, clock, snap([DOWN]), 30)
    assert len(dangers(outbox)) == 1


def test_mute_ending_inside_a_window_still_holds(env):
    rec, SL, clock, outbox = env
    mute(SL, T0 + timedelta(minutes=2))
    window(rec, minutes=20)
    open_incident(rec, clock, DOWN)
    tick(rec, clock, snap([DOWN]), 20)           # mute over, window on
    assert dangers(outbox) == []
    tick(rec, clock, snap([DOWN]), 30)
    assert len(dangers(outbox)) == 1


def test_ack_inside_a_window_skips_the_email_for_good_and_undo_rearms_it(env):
    rec, SL, clock, outbox = env
    window(rec, minutes=5)
    open_incident(rec, clock, DOWN)
    assert rec.ack(KEY, "wife") is True
    tick(rec, clock, snap([DOWN]), 20)           # window over: acked, so no email
    assert dangers(outbox) == []
    rec.unack(KEY)
    tick(rec, clock, snap([DOWN]))
    assert len(dangers(outbox)) == 1


def test_escalation_inside_a_window_drops_the_ack_but_stays_held(env):
    rec, SL, clock, outbox = env
    window(rec, "slow_speed:isp", minutes=5)
    open_incident(rec, clock, SLOW)
    rec.ack("slow_speed:isp", "alex")
    tick(rec, clock, snap([{**SLOW, "severity": "crit"}]))
    with SL() as db:
        inc = db.query(Incident).one()
        assert inc.acked_by is None and not inc.emailed
    assert dangers(outbox) == []
    tick(rec, clock, snap([{**SLOW, "severity": "crit"}]), 12)
    assert len(dangers(outbox)) == 1


def test_digest_lists_open_incidents_in_maintenance(env):
    rec, SL, clock, outbox = env
    window(rec, minutes=60)
    open_incident(rec, clock, DOWN)
    incs = rec.mark_maintenance(rec.recent_incidents(24))
    assert incs[0]["maint"] is True
    raw = emails.digest(snap(), incs, "alex@example.com", clock["now"], "America/New_York").decode()
    assert "in maintenance" in raw.lower()


def test_digest_sent_by_the_recorder_says_in_maintenance(env):
    rec, SL, clock, outbox = env
    clock["now"] = T0 + timedelta(hours=2)       # 7:00 AM New York, past the 6:30 digest
    with SL() as db:
        settingsstore.put(db, "digest_last_sent", "2026-10-02")
    window(rec, minutes=60)
    open_incident(rec, clock, DOWN)
    with SL() as db:
        settingsstore.put(db, "digest_last_sent", "")
    tick(rec, clock, snap([DOWN]))
    (digest,) = mails(outbox)                    # the Danger email is held
    assert "in maintenance" in digest.get_payload(0).get_payload(decode=True).decode().lower()


# ---------- page + endpoints ----------
def signed_in(app, name="alex", admin=False, discreet=False):
    make_user(app, name)
    with app.state.SessionLocal() as db:
        db.query(User).filter(User.username == name).update({"is_admin": admin})
        if discreet:
            settingsstore.put(db, "discreet", "1")
        db.commit()
    c = TestClient(app)
    sign_in(c, name)
    return c


def rows(app):
    with app.state.SessionLocal() as db:
        return db.query(MaintenanceWindow).order_by(MaintenanceWindow.id).all()


def test_page_and_endpoints_need_sign_in(client):
    assert client.get("/maintenance", follow_redirects=False).status_code == 303
    assert client.post("/api/maintenance", data={"target": "*", "minutes": "30"}, headers=ORIGIN).status_code == 401
    assert client.post("/api/maintenance/1/cancel", headers=ORIGIN).status_code == 401


def test_any_user_can_add_and_cancel_a_window(app):
    c = signed_in(app)                           # not an admin
    r = c.post("/api/maintenance", data={"target": KEY, "minutes": "120", "note": "new drive"}, headers=ORIGIN,
               follow_redirects=False)
    assert r.status_code == 303
    (w,) = rows(app)
    assert w.target == KEY and w.created_by == "alex" and w.note == "new drive" and not w.cancelled
    assert w.end - w.start == timedelta(hours=2)
    page = c.get("/maintenance").text
    assert "new drive" in page and "Active" in page
    assert c.post(f"/api/maintenance/{w.id}/cancel", headers=ORIGIN, follow_redirects=False).status_code == 303
    assert rows(app)[0].cancelled
    assert c.post("/api/maintenance/999/cancel", headers=ORIGIN).status_code == 404


def test_custom_start_and_end_in_home_time(app):
    c = signed_in(app)
    r = c.post("/api/maintenance", data={"target": "*", "start": "2030-01-05T22:00", "minutes": "custom",
                                         "end": "2030-01-06T01:30"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    (w,) = rows(app)
    assert (w.start.hour, w.end.hour, w.end.minute) == (3, 6, 30)   # EST = UTC-5, stored naive UTC


def test_validation(app):
    c = signed_in(app)
    bad = [{"target": "*", "minutes": "custom", "start": "2030-01-05T22:00", "end": "2030-01-05T21:00"},   # end before start
           {"target": "*", "minutes": "custom", "start": "2030-01-01T00:00", "end": "2030-01-16T00:00"},   # over 14 days
           {"target": "*", "minutes": "30", "note": "x" * 141},
           {"target": "not a key", "minutes": "30"},
           {"target": "*", "minutes": "45"},
           {"target": "*", "minutes": "custom", "end": "soon"}]
    for data in bad:
        assert c.post("/api/maintenance", data=data, headers=ORIGIN).status_code == 400, data
    assert rows(app) == []


def test_cross_site_is_refused(app):
    c = signed_in(app)
    evil = {"Origin": "https://evil.example"}
    assert c.post("/api/maintenance", data={"target": "*", "minutes": "30"}, headers=evil).status_code == 403
    c.post("/api/maintenance", data={"target": "*", "minutes": "30"}, headers=ORIGIN)
    assert c.post(f"/api/maintenance/{rows(app)[0].id}/cancel", headers=evil).status_code == 403
    assert len(rows(app)) == 1 and not rows(app)[0].cancelled


def test_discreet_hides_and_refuses_the_hidden_apps_windows(app):
    app.state.recorder.add_window("app_down:jobs", app.state.recorder.now(), app.state.recorder.now() + timedelta(hours=1),
                                  "secret note", "alex")
    c = signed_in(app, discreet=True)
    assert "secret note" not in c.get("/maintenance").text
    assert c.post(f"/api/maintenance/{rows(app)[0].id}/cancel", headers=ORIGIN).status_code == 404
    assert c.post("/api/maintenance", data={"target": "app_down:jobs", "minutes": "30"}, headers=ORIGIN).status_code == 404
    assert c.get("/api/snapshot").json()["maintenance"] == []


def test_snapshot_marks_alerts_in_maintenance(app, builder):
    builder.snapshot = {**snap([DOWN, SLOW])}
    app.state.store.refresh()
    c = signed_in(app)
    c.post("/api/maintenance", data={"target": KEY, "minutes": "30"}, headers=ORIGIN)
    body = c.get("/api/snapshot").json()
    by = {f"{a['kind']}:{a['target']}": a for a in body["alerts"]}
    assert by[KEY]["maint_label"].startswith("In maintenance until ") and by[KEY]["maint_until"]
    assert by["slow_speed:isp"]["maint_label"] == ""
    assert [w["target"] for w in body["maintenance"]] == [KEY]


def test_dropdown_links_to_the_page_and_page_prefills_target(app):
    c = signed_in(app)
    assert 'href="/maintenance"' in c.get("/profile").text
    assert f'value="{KEY}" selected' in c.get(f"/maintenance?target={KEY}").text
