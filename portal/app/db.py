"""SQLite (WAL) engine, session factory and the per-request DB dependency."""
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{data_dir / 'portal.db'}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def make_sessionmaker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request):
    db = request.app.state.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrate(engine) -> None:
    """Small in-place upgrades for databases created by older portal versions."""
    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    if "is_admin" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
    if "google_email" not in cols:
        with engine.begin() as conn:   # SQLite can't ADD a UNIQUE column, so the uniqueness is an index
            conn.execute(text("ALTER TABLE users ADD COLUMN google_email VARCHAR(254)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_email ON users (google_email)"))
    if "sessions" in inspect(engine).get_table_names() and "last_seen" not in {c["name"] for c in inspect(engine).get_columns("sessions")}:
        with engine.begin() as conn:   # signed-in people aren't kicked out by the upgrade: their idle clock starts now
            conn.execute(text("ALTER TABLE sessions ADD COLUMN last_seen DATETIME"))
            conn.execute(text("UPDATE sessions SET last_seen = CURRENT_TIMESTAMP"))
    tables = inspect(engine).get_table_names()
    if "incidents" in tables and "clear_sent" not in {c["name"] for c in inspect(engine).get_columns("incidents")}:
        with engine.begin() as conn:   # existing rows: treat their all-clears as settled
            conn.execute(text("ALTER TABLE incidents ADD COLUMN clear_sent BOOLEAN NOT NULL DEFAULT 1"))
