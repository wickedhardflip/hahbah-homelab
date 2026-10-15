import sqlite3
from dataclasses import replace

from conftest import make_user


def test_tautulli_key_is_hidden_from_repr(settings):
    s = replace(settings, tautulli_url="http://tautulli:8181", tautulli_api_key="FAKE-key-123")
    assert "FAKE-key-123" not in repr(s) and "tautulli_api_key" not in repr(s)


def test_users_are_not_admin_by_default(app):
    from app.models import User
    make_user(app)
    with app.state.SessionLocal() as db:
        assert db.query(User).one().is_admin is False


def test_migration_adds_is_admin_and_keeps_rows(settings):
    settings.data_dir.mkdir(parents=True)
    con = sqlite3.connect(settings.data_dir / "portal.db")
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50) UNIQUE NOT NULL, "
                "password_hash TEXT NOT NULL, created_at DATETIME NOT NULL)")
    con.execute("INSERT INTO users VALUES (1, 'alex', 'x', '2026-09-30 00:00:00')")
    con.commit()
    con.close()
    from app.main import create_app
    from conftest import FakeBuilder
    app = create_app(settings, builder=FakeBuilder(), collect=False)
    from app.models import User
    with app.state.SessionLocal() as db:
        u = db.query(User).one()
        assert u.username == "alex" and u.is_admin is False


def test_set_admin_toggles_the_flag(app, settings, monkeypatch):
    from app import manage
    from app.models import User
    monkeypatch.setattr(manage, "load_settings", lambda: settings)
    make_user(app)
    out = []
    assert manage.main(["set-admin", "Alex"], out=out.append) == 0
    with app.state.SessionLocal() as db:
        assert db.query(User).one().is_admin is True
    assert manage.main(["set-admin", "alex", "--off"], out=out.append) == 0
    with app.state.SessionLocal() as db:
        assert db.query(User).one().is_admin is False
    assert manage.main(["set-admin", "nobody"], out=out.append) == 1
