"""Run the real portal on made-up data, for screenshots. Nothing here touches a real network or host.

    python tools/sample_portal.py [--port 8765] [--root PATH_TO_EXPORT]

Needs the portal's own venv (uvicorn, fastapi). Serves http://127.0.0.1:PORT with an admin account `demo` (password printed on
start; it is a throwaway used only against this process). The snapshot is mockup/snapshot.js (simulated), plus sample sports,
weather and email-recipient data. Stop with Ctrl+C. Data lives in a temp folder that is deleted on exit.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if "--root" in sys.argv:   # run against the sanitized export, so the shots show the public names
    ROOT = Path(sys.argv[sys.argv.index("--root") + 1]).resolve()
sys.path.insert(0, str(ROOT / "portal"))
PASSWORD = "sample-demo-password"


def load_snapshot() -> dict:
    js = ROOT / "mockup" / "snapshot.js"
    code = f"global.window={{}};require({json.dumps(str(js))});process.stdout.write(JSON.stringify(window.HOMELAB_SNAPSHOT))"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, check=True).stdout)


def game(day, opp, home, us, them):
    d = (datetime.now(timezone.utc) - timedelta(days=day)).strftime("%Y-%m-%dT23:00:00Z")
    return {"date": d, "opp": opp, "opp_logo": None, "home": home, "us": us, "them": them, "result": "W" if us > them else "L"}


def standings(cols, rows, title):
    return {"title": title, "cols": cols, "rows": rows}


def sample_sports() -> dict:
    nxt = {"date": (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT23:10:00Z"), "opp": "New York Yankees", "opp_logo": None, "home": True}
    base = {"live": None, "logo": None, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    wl = [["w", "W"], ["l", "L"], ["gb", "GB"]]
    row = lambda n, w, l, gb, us=False: {"name": n, "logo": None, "us": us, "w": w, "l": l, "gb": gb}
    from app.collectors.sportsteams import DEFAULT_PICKS, team_meta
    meta = {k: team_meta(k, DEFAULT_PICKS[k]) for k in DEFAULT_PICKS}
    return {
        "ballpark": {**base, "team": meta["ballpark"], "next": nxt,
                     "last": [game(5, "Tampa Bay Rays", False, 3, 5), game(4, "Tampa Bay Rays", False, 6, 2), game(3, "Baltimore Orioles", True, 4, 1),
                              game(2, "Baltimore Orioles", True, 2, 3), game(1, "Toronto Blue Jays", True, 7, 4)],
                     "standings": standings(wl, [row("Yankees", 94, 68, "-"), row("Red Sox", 89, 73, "5", True), row("Blue Jays", 85, 77, "9"),
                                                 row("Rays", 80, 82, "14"), row("Orioles", 70, 92, "24")], "AL East")},
        "arena": {**base, "team": meta["arena"], "next": {**nxt, "opp": "Miami Heat"},
                  "last": [game(6, "Orlando Magic", True, 113, 108), game(5, "Toronto Raptors", False, 99, 104), game(4, "Chicago Bulls", True, 121, 110),
                           game(3, "Indiana Pacers", False, 118, 122), game(2, "Detroit Pistons", True, 109, 101)],
                  "standings": standings(wl, [row("Celtics", 58, 24, "-", True), row("Knicks", 51, 31, "7"), row("Cavaliers", 48, 34, "10"),
                                              row("Magic", 41, 41, "17"), row("Heat", 37, 45, "21")], "East")},
        "busstop": {**base, "team": meta["busstop"], "next": {**nxt, "opp": "Boston Celtics"},
                    "last": [game(7, "Washington Wizards", True, 120, 98), game(5, "Brooklyn Nets", False, 101, 107), game(4, "Atlanta Hawks", True, 112, 109),
                             game(3, "Miami Heat", False, 95, 103), game(1, "Orlando Magic", True, 117, 111)],
                    "standings": standings(wl, [row("Hawks", 44, 38, "-"), row("Hornets", 41, 41, "3", True), row("Wizards", 25, 57, "19")], "East")},
    }


def sample_weather() -> dict:
    now = datetime.now()
    hourly = [{"t": (now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00"), "temp": 58 + (h % 6) * 2 - (h // 8), "pop": (h * 7) % 40, "code": 1 if h < 8 else 3}
              for h in range(24)]
    daily = [{"d": (now + timedelta(days=d)).strftime("%Y-%m-%d"), "hi": 66 + d % 3, "lo": 49 + d % 4, "pop": (d * 15) % 60, "code": (1, 3, 61, 2, 0, 1, 3)[d]}
             for d in range(7)]
    return {"place": "Cambridge, Massachusetts, US", "current": {"temp": 61, "code": 1, "day": True, "t": now.strftime("%Y-%m-%dT%H:%M")},
            "hourly": hourly, "daily": daily}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", help="repo folder to serve (default: this repo)")
    a = ap.parse_args()
    import uvicorn
    from app import settingsstore
    from app.auth import hash_password
    from app.config import Settings
    from app.main import create_app
    from app.models import User

    tmp = Path(tempfile.mkdtemp(prefix="sample-portal-"))
    for d in ("data", "outbox", "mail", "weather"):
        (tmp / d).mkdir()
    snap = load_snapshot()
    snap["sports"], snap["weather"] = sample_sports(), sample_weather()

    class Fake:
        def build(self):
            return snap

    s = Settings(data_dir=tmp / "data", host_proc=tmp / "proc", host_sys=tmp / "sys", disks=(("/", str(tmp)),), nic="eth0",
                 eero_igd_url="http://127.0.0.1:9/igd.xml", deploy_log=tmp / "deploy.log", apps_file=ROOT / "apps.yaml",
                 cookie_name="hl_session", cookie_domain=None, cookie_secure=False, session_days=1, collect_interval=30,
                 base_domain="example.com", home_url=f"http://127.0.0.1:{a.port}", outbox_dir=tmp / "outbox", mail_dir=tmp / "mail",
                 alert_to="you@example.com", sports_file=tmp / "weather" / "sports.json", weather_dir=tmp / "weather")
    app = create_app(s, builder=Fake(), collect=False)
    with app.state.SessionLocal() as db:
        db.add(User(username="demo", password_hash=hash_password(PASSWORD), is_admin=True))
        settingsstore.put(db, "web_recipients", "friend@example.com")
        db.commit()
    app.state.recorder.mail_dir = tmp / "mail"
    print(f"sample portal on http://127.0.0.1:{a.port}  (user demo / {PASSWORD})", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
