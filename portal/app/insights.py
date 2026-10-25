"""Insights: the disk-full forecast, the ISP report card and the weekly digest. Pure reads of what the portal already keeps
(daily_metrics, incidents); nothing here collects, calls out or writes except the optional digest message."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import emails, settingsstore
from .models import DailyMetric, Incident

WINDOW_DAYS = 30        # the recorder keeps 30 days of daily numbers (KEEP_DAYS)
MIN_POINTS = 7          # fewer days than this is a guess, not a forecast
PLAN_DEFAULT = (500, 20)
FLAT = 0.02             # percentage points per day under which a disk is "not filling"


def series(db, key: str, days: int = WINDOW_DAYS, today: date | None = None) -> list:
    """[(date, value)] for one daily metric over the last `days` days, oldest first."""
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    rows = db.execute(select(DailyMetric.day, DailyMetric.value).where(DailyMetric.key == key, DailyMetric.day >= since)
                      .order_by(DailyMetric.day)).all()
    return [(date.fromisoformat(d), float(v)) for d, v in rows]


def forecast(points: list) -> dict:
    """Straight-line fit of percent used against the day. Reports the slope and the dates the disk reaches 90% and 100%."""
    if len(points) < MIN_POINTS:
        return {"state": "short", "points": len(points)}
    x0 = points[0][0]
    xs = [(d - x0).days for d, _ in points]
    ys = [v for _, v in points]
    n, mx, my = len(xs), sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 0.0
    last_d, last_v = points[-1]
    out = {"state": "flat", "slope_per_day": round(slope, 3), "now_pct": round(last_v, 1), "points": n}
    if slope <= FLAT:
        return out
    def when(target):
        left = target - last_v
        return None if left <= 0 else (last_d + timedelta(days=round(left / slope))).isoformat()
    full, nine = when(100.0), when(90.0)
    return {**out, "state": "filling", "full_on": full, "full_in_days": round((100.0 - last_v) / slope) if full else 0, "ninety_on": nine}


def _minutes(i: Incident, now: datetime) -> int:
    return max(0, int(((i.closed_at or now) - i.opened_at).total_seconds() // 60))


def outages(db, now: datetime, days: int = WINDOW_DAYS) -> list:
    """Gateway ping losses and slow speed incidents in the window. The pings reach the Eero, not the open internet, so a
    lost ping is the home network or the line; each row says which signal saw it."""
    since = now - timedelta(days=days)
    rows = db.execute(select(Incident).where(Incident.opened_at >= since).where(
        (Incident.kind == "slow_speed") | ((Incident.kind == "host_down") & (Incident.target == "router")))
        .order_by(Incident.opened_at.desc())).scalars().all()
    return [{"opened_at": i.opened_at.replace(tzinfo=timezone.utc).isoformat(), "minutes": _minutes(i, now), "open": i.closed_at is None,
             "kind": "outage" if i.kind == "host_down" else "slow", "saw": i.message} for i in rows]


def grade(ratio: float | None, outage_min: int, slow: int) -> str:
    """Letter for the month: speed held against the plan, minus a little for outage minutes and slow days."""
    if ratio is None:
        return "-"
    score = 100 * min(ratio, 1.0) - min(30, outage_min / 5) - 2 * slow
    for floor, letter in ((97, "A+"), (93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"), (77, "C+"), (73, "C"), (70, "C-"), (60, "D")):
        if score >= floor:
            return letter
    return "F"


def isp_report(db, now: datetime, plan: tuple = PLAN_DEFAULT, today: date | None = None) -> dict:
    down, up = series(db, "speed_down", today=today), series(db, "speed_up", today=today)
    items = outages(db, now)
    out_min = sum(o["minutes"] for o in items if o["kind"] == "outage")
    slow = sum(1 for o in items if o["kind"] == "slow")
    avg = lambda s: round(sum(v for _, v in s) / len(s), 1) if s else None
    ad, au = avg(down), avg(up)
    ratio = ad / plan[0] if ad is not None and plan[0] else None
    return {"plan": {"down": plan[0], "up": plan[1]}, "tests": len(down), "avg_down": ad, "avg_up": au,
            "worst_down": min((v for _, v in down), default=None), "held_pct": round(100 * ratio) if ratio is not None else None,
            "up_held_pct": round(100 * au / plan[1]) if au is not None and plan[1] else None,
            "outage_minutes": out_min, "slow_days": slow, "grade": grade(ratio, out_min, slow), "outages": items,
            "speed": [{"day": d.isoformat(), "down": v} for d, v in down]}


def plan_from(db) -> tuple:
    def num(key, default):
        try:
            n = int(settingsstore.get(db, key))
            return n if n > 0 else default
        except ValueError:
            return default
    return num("isp_plan_down", PLAN_DEFAULT[0]), num("isp_plan_up", PLAN_DEFAULT[1])


def report(db, now: datetime, tz: str) -> dict:
    today = now.astimezone(ZoneInfo(tz)).date()
    pts = series(db, "nas_used_pct", today=today)
    return {"forecast": {**forecast(pts), "series": [{"day": d.isoformat(), "v": v} for d, v in pts]}, "isp": isp_report(db, now, plan_from(db), today)}


# ---------- weekly digest ----------
def weekly_message(rep: dict, to: str, now: datetime, tz: str) -> bytes:
    local = now.astimezone(ZoneInfo(tz))
    isp, fc = rep["isp"], rep["forecast"]
    if fc["state"] == "filling":
        disk = f"fills about {fc['full_on']} ({fc['now_pct']}% used now)"
    else:
        disk = {"flat": f"steady at {fc.get('now_pct')}% used", "short": "needs a week of history"}[fc["state"]]
    pairs = [("Report card grade", isp["grade"]),
             ("Download vs plan", f"{isp['held_pct']}% ({isp['avg_down']} of {isp['plan']['down']} Mbps)" if isp["avg_down"] is not None else "no tests yet"),
             ("Outage minutes, 30 days", str(isp["outage_minutes"])), ("Slow speed days, 30 days", str(isp["slow_days"])),
             ("NAS disk", disk)]
    title = f"Week of {local:%B} {local.day}"
    body = emails._banner(emails.GOOD, "Your week at HAHBAH", title) + emails._section("Insights", emails.GOOD, "Internet and disk", emails._rows(pairs))
    text = "\n".join([f"HAHBAH, {title}", ""] + [f"{k}: {v}" for k, v in pairs])
    return emails._message(to, f"Your week at HAHBAH: grade {isp['grade']}", text, emails._page(emails._sign() + body, f"Grade {isp['grade']}"))


def weekly_due(st: dict, local: datetime) -> bool:
    """Monday from 7:00 home time, once per ISO week, and only if switched on (off unless Settings says otherwise)."""
    y, w, _ = local.isocalendar()
    return (st.get("insights_digest") == "1" and local.weekday() == 0 and local.hour >= 7
            and st.get("insights_digest_week") != f"{y}-{w:02d}")
