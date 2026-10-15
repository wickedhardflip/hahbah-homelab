from app import manage


def run(tmp_path, monkeypatch, argv, answers):
    monkeypatch.setenv("PORTAL_DATA_DIR", str(tmp_path / "data"))
    it = iter(answers)
    lines = []
    code = manage.main(argv, prompt=lambda _msg: next(it), out=lines.append)
    return code, lines


def test_create_user_then_sign_in_works(tmp_path, monkeypatch):
    code, lines = run(tmp_path, monkeypatch, ["create-user", "Alex"], ["a-long-password-1", "a-long-password-1"])
    assert code == 0 and lines[-1] == "Done."
    from app.auth import authenticate
    from app.db import make_engine, make_sessionmaker
    with make_sessionmaker(make_engine(tmp_path / "data"))() as db:
        assert authenticate(db, "alex", "a-long-password-1") is not None


def test_short_or_mismatched_passwords_are_refused(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, ["create-user", "b"], ["short", "short"])[0] == 1
    assert run(tmp_path, monkeypatch, ["create-user", "b"], ["a-long-password-1", "a-long-password-2"])[0] == 1


def test_set_password_signs_out_everywhere(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, ["create-user", "alex"], ["a-long-password-1"] * 2)
    from app.auth import authenticate, create_session
    from app.db import make_engine, make_sessionmaker
    from app.models import Session
    SL = make_sessionmaker(make_engine(tmp_path / "data"))
    with SL() as db:
        create_session(db, authenticate(db, "alex", "a-long-password-1"), 30)
    assert run(tmp_path, monkeypatch, ["set-password", "alex"], ["a-long-password-2"] * 2)[0] == 0
    with SL() as db:
        assert db.query(Session).count() == 0
        assert authenticate(db, "alex", "a-long-password-2") is not None


def test_usage_on_bad_arguments(tmp_path, monkeypatch):
    code, lines = run(tmp_path, monkeypatch, ["delete-everything"], [])
    assert code == 2 and "create-user" in lines[0]


def _google_of(tmp_path, name):
    from app.db import make_engine, make_sessionmaker
    from app.models import User
    with make_sessionmaker(make_engine(tmp_path / "data"))() as db:
        return db.query(User).filter(User.username == name).one().google_email


def test_link_google_stores_lower_case(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, ["create-user", "alex"], ["a-long-password-1"] * 2)
    code, lines = run(tmp_path, monkeypatch, ["link-google", "Alex", " Alex@Example.COM "], [])
    assert code == 0 and _google_of(tmp_path, "alex") == "alex@example.com"
    assert "alex@example.com" in lines[-1]


def test_link_google_refuses_an_email_linked_to_someone_else(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, ["create-user", "alex"], ["a-long-password-1"] * 2)
    run(tmp_path, monkeypatch, ["create-user", "sam"], ["a-long-password-1"] * 2)
    run(tmp_path, monkeypatch, ["link-google", "alex", "alex@example.com"], [])
    code, lines = run(tmp_path, monkeypatch, ["link-google", "sam", "ALEX@example.com"], [])
    assert code == 1 and "already linked to alex" in lines[-1] and _google_of(tmp_path, "sam") is None


def test_link_google_needs_a_real_user_and_an_email(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, ["create-user", "alex"], ["a-long-password-1"] * 2)
    assert run(tmp_path, monkeypatch, ["link-google", "nobody", "x@example.com"], [])[0] == 1
    assert run(tmp_path, monkeypatch, ["link-google", "alex", "not-an-email"], [])[0] == 1


def test_unlink_google(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, ["create-user", "alex"], ["a-long-password-1"] * 2)
    run(tmp_path, monkeypatch, ["link-google", "alex", "alex@example.com"], [])
    code, _ = run(tmp_path, monkeypatch, ["unlink-google", "alex"], [])
    assert code == 0 and _google_of(tmp_path, "alex") is None


def test_import_legacymonitor_history(tmp_path, monkeypatch):
    import sqlite3
    wt = tmp_path / "wt.db"
    c = sqlite3.connect(wt)
    c.execute("CREATE TABLE speed_tests (id INTEGER PRIMARY KEY, test_type VARCHAR(20), download_mbps FLOAT, upload_mbps FLOAT, latency_ms FLOAT, tested_at DATETIME)")
    c.execute("CREATE TABLE disk_usages (id INTEGER PRIMARY KEY, device_id INTEGER, total_bytes INTEGER, used_bytes INTEGER, free_bytes INTEGER, usage_percent FLOAT, checked_at DATETIME)")
    c.executemany("INSERT INTO speed_tests (test_type, download_mbps, upload_mbps, latency_ms, tested_at) VALUES (?,?,?,?,?)",
                  [("internet", 160.4, 131.9, 15, "2026-09-30 04:10:00"), ("lan", 101, 0, None, "2026-09-30 04:15:00"),
                   ("internet", 203.0, 189.2, 15, "2026-10-01 04:10:00")])
    c.execute("INSERT INTO disk_usages (device_id, total_bytes, used_bytes, free_bytes, usage_percent, checked_at) VALUES (2,1,1,1,56.6,'2026-10-01 04:00:00')")
    c.commit(); c.close()
    code, lines = run(tmp_path, monkeypatch, ["import-legacymonitor", str(wt)], [])
    assert code == 0 and "5 daily numbers" in lines[-1]
    from app.db import make_engine, make_sessionmaker
    from app.models import DailyMetric
    with make_sessionmaker(make_engine(tmp_path / "data"))() as db:
        got = {(m.day, m.key): m.value for m in db.query(DailyMetric).all()}
    assert got[("2026-10-01", "speed_down")] == 203.0 and got[("2026-09-30", "speed_down")] == 160.4
    assert got[("2026-10-01", "nas_used_pct")] == 56.6 and ("2026-09-30", "speed_up") in got
