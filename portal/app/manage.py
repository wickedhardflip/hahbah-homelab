"""Portal admin commands. Run inside the container (the password prompt is hidden):

    docker exec -it homelab-portal-1 python -m app.manage create-user <name>
    docker exec -it homelab-portal-1 python -m app.manage set-password <name>
    docker exec homelab-portal-1 python -m app.manage set-admin <name> [--off]
    docker exec homelab-portal-1 python -m app.manage link-google <name> <google email>
    docker exec homelab-portal-1 python -m app.manage unlink-google <name>
    docker exec homelab-portal-1 python -m app.manage import-legacymonitor <copy of legacymonitor.db>
"""
import getpass
import sys

from .auth import hash_password, normalize
from .config import load_settings
from .db import Base, make_engine, make_sessionmaker, migrate
from .models import Session, User

MIN_LENGTH = 12


def main(argv, prompt=getpass.getpass, out=print) -> int:
    if argv and argv[0] == "import-legacymonitor" and len(argv) == 2:
        return _import_legacymonitor(argv[1], out)
    if argv and argv[0] == "link-google" and len(argv) == 3:
        return _link_google(normalize(argv[1]), argv[2].strip().lower(), out)
    if argv and argv[0] == "unlink-google" and len(argv) == 2:
        return _link_google(normalize(argv[1]), None, out)
    if argv and argv[0] == "set-admin" and len(argv) in (2, 3) and (len(argv) == 2 or argv[2] == "--off"):
        return _set_admin(normalize(argv[1]), len(argv) == 2, out)
    if len(argv) != 2 or argv[0] not in ("create-user", "set-password"):
        out(__doc__)
        return 2
    command, name = argv[0], normalize(argv[1])
    password = prompt("New password (hidden): ")
    if password != prompt("Same password again: "):
        out("Passwords don't match. Nothing changed.")
        return 1
    if len(password) < MIN_LENGTH:
        out(f"Use at least {MIN_LENGTH} characters. Nothing changed.")
        return 1
    engine = make_engine(load_settings().data_dir)
    Base.metadata.create_all(engine)
    migrate(engine)
    with make_sessionmaker(engine)() as db:
        user = db.query(User).filter(User.username == name).first()
        if command == "create-user":
            if user:
                out(f"{name} already exists. Use set-password.")
                return 1
            db.add(User(username=name, password_hash=hash_password(password)))
        else:
            if not user:
                out(f"There's no user called {name}.")
                return 1
            user.password_hash = hash_password(password)
            db.query(Session).filter(Session.user_id == user.id).delete()  # sign out everywhere
        db.commit()
    out("Done.")
    return 0



def _set_admin(name: str, on: bool, out) -> int:
    engine = make_engine(load_settings().data_dir)
    Base.metadata.create_all(engine)
    migrate(engine)
    with make_sessionmaker(engine)() as db:
        user = db.query(User).filter(User.username == name).first()
        if not user:
            out(f"There's no user called {name}.")
            return 1
        user.is_admin = on
        db.commit()
    out(f"{name} is {'now' if on else 'no longer'} an admin.")
    return 0


def _link_google(name: str, email: str | None, out) -> int:
    if email is not None and ("@" not in email or " " in email):
        out(f"{email!r} doesn't look like an email address. Nothing changed.")
        return 1
    engine = make_engine(load_settings().data_dir)
    Base.metadata.create_all(engine)
    migrate(engine)
    with make_sessionmaker(engine)() as db:
        user = db.query(User).filter(User.username == name).first()
        if not user:
            out(f"There's no user called {name}.")
            return 1
        if email is not None:
            other = db.query(User).filter(User.google_email == email, User.id != user.id).first()
            if other:
                out(f"{email} is already linked to {other.username}. Nothing changed.")
                return 1
        user.google_email = email
        db.commit()
    out(f"{name} can now sign in with Google as {email}." if email else f"{name} is no longer linked to a Google account.")
    return 0


def _import_legacymonitor(path: str, out) -> int:
    """One-off: LegacyMonitor's daily internet speed tests and NAS volume use become the trend lines' first 30 days."""
    import sqlite3
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from .models import DailyMetric
    tz = ZoneInfo(load_settings().home_tz)
    day = lambda t: datetime.fromisoformat(t.split(".")[0]).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
    rows = {}
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    for d, u, at in src.execute("SELECT download_mbps, upload_mbps, tested_at FROM speed_tests WHERE test_type = 'internet' "
                                "AND download_mbps IS NOT NULL ORDER BY tested_at"):
        rows[(day(at), "speed_down")] = round(d, 1)
        rows[(day(at), "speed_up")] = round(u, 1)
    for pct, at in src.execute("SELECT usage_percent, checked_at FROM disk_usages ORDER BY checked_at"):
        rows[(day(at), "nas_used_pct")] = round(pct, 1)
    src.close()
    engine = make_engine(load_settings().data_dir)
    Base.metadata.create_all(engine)
    migrate(engine)
    with make_sessionmaker(engine)() as db:
        for (d, k), v in rows.items():
            if db.get(DailyMetric, (d, k)) is None:   # never overwrite what the portal recorded itself
                db.add(DailyMetric(day=d, key=k, value=v))
        db.commit()
    out(f"Imported {len(rows)} daily numbers from LegacyMonitor.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
