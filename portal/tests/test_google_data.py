from sqlalchemy import create_engine, inspect, text

from app.db import migrate


def test_migrate_adds_google_email_once(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:   # a users table from before slice 4
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50) NOT NULL UNIQUE, "
                          "password_hash TEXT NOT NULL, is_admin BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME NOT NULL)"))
    migrate(engine)
    migrate(engine)   # second run is a no-op
    assert "google_email" in {c["name"] for c in inspect(engine).get_columns("users")}
    assert any(ix["unique"] and ix["column_names"] == ["google_email"] for ix in inspect(engine).get_indexes("users"))


def test_migrate_adds_session_last_seen_and_starts_the_idle_clock(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old2.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50) NOT NULL UNIQUE, password_hash TEXT NOT NULL, "
                          "is_admin BOOLEAN NOT NULL DEFAULT 0, google_email VARCHAR(254), created_at DATETIME NOT NULL)"))
        conn.execute(text("CREATE TABLE sessions (id VARCHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, created_at DATETIME NOT NULL, "
                          "expires_at DATETIME NOT NULL)"))
        conn.execute(text("INSERT INTO sessions VALUES ('a', 1, '2026-01-01 00:00:00', '2099-01-01 00:00:00')"))
    migrate(engine)
    migrate(engine)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT last_seen FROM sessions")).scalar() is not None   # existing sign-ins aren't kicked out
