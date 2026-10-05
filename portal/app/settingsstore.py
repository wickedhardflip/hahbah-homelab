"""Portal settings kept in the database (changed on the admin Settings page)."""
from .models import Setting

DEFAULTS = {
    "discreet": "0",          # hide Example App everywhere in the portal
    "digest_enabled": "1",
    "digest_time": "06:30",   # home time
    "alerts_enabled": "1",    # instant Danger emails + all-clears
    "mute_until": "",         # ISO UTC; instant alerts muted until then
    "alerts_off": "",         # comma list: allowlisted addresses that don't get instant alerts
    "report_off": "",         # comma list: allowlisted addresses that don't get the morning report
    "web_recipients": "",     # comma list: addresses added on the Settings page (max 5; also in /mail/recipients.json)
    "owners_off": "",         # comma list: server-file addresses removed on the Settings page (also in /mail/recipients.json)
    "mail_primary": "",       # the address chosen as primary on the Settings page ("" = the first one on the list)
    "weather_name": "Cambridge, Massachusetts, US",   # Settings geocodes a city/ZIP to these three
    "weather_lat": "42.3736",
    "weather_lon": "-71.1097",
    "sports_ballpark": "mlb:111",   # "league:team" for the ballpark card (Settings > Sports)
    "sports_arena": "nba:BOS",      # basketball or hockey team for the arena card
    "sports_busstop": "nba:CHA",    # any league's team for the remote bus stop, or "none" to hide it
    "idle_minutes": "480",    # sign a session out after this many minutes without a request (15..10080)
    "digest_last_sent": "",   # YYYY-MM-DD (internal)
}
IDLE_MIN, IDLE_MAX, IDLE_DEFAULT = 15, 10080, 480
DIGEST_TIMES = ("05:30", "06:00", "06:30", "07:00", "07:30", "08:00")


def parse_idle(value) -> int | None:
    """The idle timeout in minutes if `value` is a whole number in range, else None."""
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return n if IDLE_MIN <= n <= IDLE_MAX else None


def idle_minutes(db) -> int:
    return parse_idle(get(db, "idle_minutes")) or IDLE_DEFAULT


def get_all(db) -> dict:
    out = dict(DEFAULTS)
    out.update({s.key: s.value for s in db.query(Setting).all()})
    return out


def get(db, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else DEFAULTS.get(key, "")


def put(db, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()
