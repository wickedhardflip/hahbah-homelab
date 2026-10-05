from datetime import datetime, timedelta, timezone
from email import message_from_bytes

import pytest

from app.db import Base, make_engine, make_sessionmaker
from app.models import DailyMetric, Incident
from app.recorder import Recorder
from app import settingsstore

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)   # 5:00 AM in New York


@pytest.fixture
def env(tmp_path):
    engine = make_engine(tmp_path / "data")
    Base.metadata.create_all(engine)
    SL = make_sessionmaker(engine)
    clock = {"now": T0}
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    rec = Recorder(SL, outbox, "alex@example.com", tz="America/New_York", now=lambda: clock["now"])
    return rec, SL, clock, outbox


def snap(alerts=(), metrics=None):
    return {"alerts": list(alerts), "metrics": metrics or {}, "nodes": [], "links": [], "edge": {}, "layout": {"lines": []}}


DOWN = {"id": "live-health-plex", "severity": "crit", "target": "plex", "kind": "app_down", "message": "Plex didn't answer its health check."}
SLOW = {"id": "live-speed", "severity": "warn", "target": "isp", "kind": "slow_speed", "message": "Slow."}


def tick(rec, clock, s, n=1):
    for _ in range(n):
        rec.observe(s)
        clock["now"] += timedelta(seconds=30)


def mails(outbox):
    return [message_from_bytes(p.read_bytes()) for p in sorted(outbox.glob("*.eml"), key=lambda p: int(p.stem.split("-")[-1]))]


def test_one_bad_check_is_not_an_incident(env):
    rec, SL, clock, outbox = env
    tick(rec, clock, snap([DOWN]))
    tick(rec, clock, snap())
    with SL() as db:
        assert db.query(Incident).count() == 0
    assert mails(outbox) == []


def test_danger_opens_after_two_checks_emails_then_all_clear_after_ten_quiet_minutes(env):
    rec, SL, clock, outbox = env
    tick(rec, clock, snap([DOWN]), 3)
    with SL() as db:
        inc = db.query(Incident).one()
        assert inc.key == "app_down:plex" and inc.closed_at is None and inc.emailed
    tick(rec, clock, snap(), 2)
    with SL() as db:
        assert db.query(Incident).one().closed_at is not None
    assert [m["Subject"] for m in mails(outbox) if "All clear" in m["Subject"]] == []   # not yet: could still flap back
    tick(rec, clock, snap(), 22)                                                        # 11 more quiet minutes
    subjects = [m["Subject"] for m in mails(outbox)]
    assert len(subjects) == 2 and "Danger" in subjects[0] and "All clear" in subjects[1]
    assert all(m["To"] == "alex@example.com" for m in mails(outbox))


def test_caution_is_recorded_but_not_emailed(env):
    rec, SL, clock, outbox = env
    tick(rec, clock, snap([SLOW]), 3)
    with SL() as db:
        assert db.query(Incident).one().severity == "warn"
    assert mails(outbox) == []


def test_flapping_within_ten_minutes_is_one_incident_and_one_email(env):
    rec, SL, clock, outbox = env
    for _ in range(3):
        tick(rec, clock, snap([DOWN]), 2)
        tick(rec, clock, snap(), 2)
    with SL() as db:
        assert db.query(Incident).count() == 1
    assert len([m for m in mails(outbox) if "Danger" in m["Subject"]]) == 1


def test_a_relapse_after_the_all_clear_is_emailed_again(env):
    rec, SL, clock, outbox = env
    tick(rec, clock, snap([DOWN]), 3)
    tick(rec, clock, snap(), 26)            # closed, then 10+ quiet minutes: all clear sent
    tick(rec, clock, snap([DOWN]), 3)       # back again: Alex must hear about it
    assert len([m for m in mails(outbox) if "Danger" in m["Subject"]]) == 2


def test_a_danger_that_started_while_muted_is_emailed_after_unmute(env):
    rec, SL, clock, outbox = env
    with SL() as db:
        settingsstore.put(db, "mute_until", (T0 + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    tick(rec, clock, snap([DOWN]), 4)       # 2 minutes in: opened while muted
    assert mails(outbox) == []
    tick(rec, clock, snap([DOWN]), 8)       # mute over, still down
    assert len([m for m in mails(outbox) if "Danger" in m["Subject"]]) == 1


def test_no_email_is_queued_when_the_save_fails(env, monkeypatch):
    rec, SL, clock, outbox = env
    import app.recorder as r
    def boom(*a, **k):
        raise RuntimeError("digest broke")
    monkeypatch.setattr(r.emails, "digest", boom)
    clock["now"] = datetime(2026, 10, 2, 10, 31, tzinfo=timezone.utc)   # digest time: the digest step raises
    for _ in range(8):
        try:
            rec.observe(snap([DOWN]))
        except RuntimeError:
            pass
        clock["now"] += timedelta(seconds=30)
    assert len([m for m in mails(outbox) if "Danger" in m["Subject"]]) == 1


def test_grey_station_keeps_its_incident_open(env):
    rec, SL, clock, outbox = env
    s_down = snap([DOWN]); s_down["nodes"] = [{"id": "plex", "status": "crit"}]
    tick(rec, clock, s_down, 3)
    s_grey = snap(); s_grey["nodes"] = [{"id": "plex", "status": "unknown"}]   # the check itself stopped reporting
    tick(rec, clock, s_grey, 30)
    with SL() as db:
        assert db.query(Incident).one().closed_at is None
    assert not [m for m in mails(outbox) if "All clear" in m["Subject"]]


def test_muted_or_disabled_alerts_send_nothing(env):
    rec, SL, clock, outbox = env
    with SL() as db:
        settingsstore.put(db, "mute_until", (T0 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    tick(rec, clock, snap([DOWN]), 3)
    assert mails(outbox) == []
    with SL() as db:
        assert db.query(Incident).count() == 1   # still recorded


def test_daily_numbers_latest_wins_and_old_rows_are_pruned(env):
    rec, SL, clock, outbox = env
    with SL() as db:
        db.add(DailyMetric(day="2026-08-01", key="speed_down", value=1.0))
        db.commit()
    tick(rec, clock, snap(metrics={"speed_down": 190.0, "nas_used_pct": 57}))
    tick(rec, clock, snap(metrics={"speed_down": 203.0, "nas_used_pct": 57}))
    t = rec.trends()
    assert t["speed_down"] == [{"d": "2026-10-02", "v": 203.0}] and t["nas_used_pct"][0]["v"] == 57
    with SL() as db:
        assert db.query(DailyMetric).filter(DailyMetric.day == "2026-08-01").count() == 0


def test_digest_goes_out_once_at_its_time(env):
    rec, SL, clock, outbox = env
    tick(rec, clock, snap())                                       # 5:00 AM: too early
    assert mails(outbox) == []
    clock["now"] = datetime(2026, 10, 2, 10, 31, tzinfo=timezone.utc)   # 6:31 AM
    tick(rec, clock, snap(), 3)
    digests = [m for m in mails(outbox) if "HAHBAH" in m["Subject"]]
    assert len(digests) == 1
    html = [p for p in digests[0].walk() if p.get_content_type() == "text/html"][0].get_payload(decode=True).decode()
    assert "ALL ABOARD" in html and "Plex backups" in html


def test_digest_can_be_turned_off(env):
    rec, SL, clock, outbox = env
    with SL() as db:
        settingsstore.put(db, "digest_enabled", "0")
    clock["now"] = datetime(2026, 10, 2, 10, 31, tzinfo=timezone.utc)
    tick(rec, clock, snap())
    assert mails(outbox) == []


def test_incident_stays_open_while_its_source_is_not_reporting(env):
    rec, SL, clock, outbox = env
    m = {"severity": "crit", "kind": "mount_missing", "target": "m_video", "message": "/mnt/video is missing."}
    s = snap([m]); s["live"] = ["mounts"]
    tick(rec, clock, s, 3)
    s2 = snap(); s2["live"] = ["pings"]          # mounts file stale: no alert, but no news either
    tick(rec, clock, s2, 30)
    with SL() as db:
        assert db.query(Incident).one().closed_at is None


def test_each_address_gets_only_the_kinds_it_wants(tmp_path):
    engine = make_engine(tmp_path / "data")
    Base.metadata.create_all(engine)
    SL = make_sessionmaker(engine)
    clock = {"now": T0}
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    rec = Recorder(SL, outbox, "alex@example.com, Wife@example.com", tz="America/New_York", now=lambda: clock["now"])
    with SL() as db:
        settingsstore.put(db, "report_off", "alex@example.com")
        settingsstore.put(db, "alerts_off", "wife@example.com")
    assert rec.picks("danger") == ("alex@example.com",) and rec.picks("digest") == ("wife@example.com",)
    assert rec.picks("test") == ("alex@example.com", "wife@example.com")
    tick(rec, clock, snap([DOWN]), 3)
    (m,) = mails(outbox)
    assert m["X-HAHBAH-To"] == "alex@example.com" and m["From"] == "HAHBAH <alex@example.com>"
    with SL() as db:
        settingsstore.put(db, "alerts_off", "alex@example.com,wife@example.com")
    assert rec.picks("clear") == ()
