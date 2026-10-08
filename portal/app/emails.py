"""HAHBAH-themed emails: the morning digest and instant Danger / All-clear alerts.
Table-based HTML with inline styles (what email clients actually render), plus a plain-text part."""
import html
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from zoneinfo import ZoneInfo

CREAM, PAPER, INK, MUTED, GREEN, FRAME = "#F1ECE0", "#EFECE3", "#1B1D22", "#646871", "#4E6E58", "#2A2A2A"
GOOD, WARN, CRIT = "#1F7A45", "#9A5800", "#C42B2B"
FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', Menlo, Consolas, monospace"
LINE_COLORS = {"red": "#D8322E", "green": "#16854A", "orange": "#E27B0C", "blue": "#1F5FBF", "purple": "#7E3F98",
               "teal": "#0B7B7E", "silver": "#7A838C", "gold": "#A88625"}
THEME = {  # same themed names as the dashboard
    "app_down": "Part suspended", "host_down": "Station closed", "mount_missing": "Shuttle buses", "disk_high": "Minor delays",
    "smart_warn": "Signal problems", "slow_speed": "Slow zone", "mem_high": "Crowding", "os_eol": "Track work",
    "unmonitored": "No timetable", "cert_expiring": "Ticket expiring", "domain_expiring": "Lease ending", "dns_drift": "Wrong platform",
    "deploy_failed": "Signal failure", "raid_degraded": "Single tracking", "backup_stale": "Missed connection", "source_down": "No timetable",
}
SEV_WORD = {"crit": "Danger", "warn": "Caution"}
SEV_COLOR = {"crit": CRIT, "warn": WARN}
e = html.escape


def _local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def _clock(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _parse(iso: str | None):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None
    except (ValueError, AttributeError):
        return None


# ---------- building blocks ----------
def _sign(small: bool = False) -> str:
    big, sub = (30, 11) if small else (46, 13)
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="border-collapse:separate;background:{FRAME};border-radius:6px">
  <tr><td style="padding:5px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr><td align="center" style="background:{GREEN};padding:{10 if small else 16}px {28 if small else 48}px;font:700 {big}px/1 {FONT};color:{CREAM};letter-spacing:-.5px">HAHBAH</td></tr>
      <tr><td align="center" style="background:{CREAM};padding:7px 0;font:700 {sub}px/1 {FONT};color:{INK};letter-spacing:3px">ALL ABOARD</td></tr>
    </table>
  </td></tr>
</table>"""


def _banner(color: str, title: str, sub: str = "") -> str:
    subline = f'<div style="font:600 13px/1.4 {MONO};color:{CREAM};opacity:.9;margin-top:6px">{e(sub)}</div>' if sub else ""
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:22px 0 6px">
  <tr><td style="background:{color};border-radius:12px;padding:18px 22px;color:{CREAM}">
    <div style="font:800 22px/1.15 {FONT};letter-spacing:1px;text-transform:uppercase">{e(title)}</div>{subline}
  </td></tr>
</table>"""


def _roundel(color: str, code: str, size: int = 34) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td align="center" valign="middle" '
            f'width="{size}" height="{size}" style="width:{size}px;height:{size}px;border-radius:{size // 2}px;background:{color};'
            f'font:800 12px/1 {FONT};color:#fff">{e(code)}</td></tr></table>')


def _section(kicker: str, color: str, title: str, body: str) -> str:
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:26px">
  <tr><td style="font:700 11px/1 {MONO};letter-spacing:3px;color:{MUTED};text-transform:uppercase;padding-bottom:8px">
    <span style="display:inline-block;width:26px;height:5px;border-radius:3px;background:{color};vertical-align:middle;margin-right:8px"></span>{e(kicker)}</td></tr>
  <tr><td style="background:#ffffff;border:1px solid #DCD7C9;border-radius:12px;padding:18px 20px">
    <div style="font:800 19px/1.2 {FONT};color:{INK};margin-bottom:10px">{e(title)}</div>{body}
  </td></tr>
</table>"""


def _rows(pairs: list) -> str:
    cells = "".join(
        f'<tr><td style="padding:6px 0;border-top:1px solid #EEE9DD;font:600 13px/1.3 {FONT};color:{MUTED};width:42%">{e(k)}</td>'
        f'<td style="padding:6px 0;border-top:1px solid #EEE9DD;font:700 14px/1.3 {MONO};color:{INK}">{e(v)}</td></tr>' for k, v in pairs)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{cells}</table>'


def _page(inner: str, preheader: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:{PAPER}">
<div style="display:none;max-height:0;overflow:hidden">{e(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAPER}">
  <tr><td align="center" style="padding:28px 14px 40px">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%">
      <tr><td>{inner}</td></tr>
      <tr><td align="center" style="padding-top:28px;font:600 12px/1.6 {FONT};color:{MUTED}">
        <a href="https://home.hahbah.com" style="color:{INK};font-weight:800">Open the dashboard</a> · home.hahbah.com (home network only)<br>
        Sent by the HAHBAH portal · turn emails on or off in Settings
      </td></tr>
    </table>
  </td></tr>
</table></body></html>"""


def _message(to: str, subject: str, text: str, html_body: str) -> bytes:
    m = EmailMessage()
    m["From"] = f"HAHBAH <{to}>"
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain="hahbah.com")
    m.set_content(text)
    m.add_alternative(html_body, subtype="html")
    return bytes(m)


# ---------- line status (same rule as the dashboard: any alert at a station on the line) ----------
def line_statuses(snap: dict) -> list:
    links = {l["id"]: l for l in snap.get("links", [])}
    out = []
    for ln in (snap.get("layout") or {}).get("lines", []):
        stations = set()
        for lid in ln.get("links", []):
            if lid in links:
                stations |= {links[lid]["a"], links[lid]["b"]}
        hits = [a for a in snap.get("alerts", []) if a.get("target") in stations and a.get("severity") in ("warn", "crit")]
        worst = "crit" if any(a["severity"] == "crit" for a in hits) else ("warn" if hits else "good")
        out.append({"id": ln["id"], "name": ln["name"], "what": ln.get("what", ""), "status": worst,
                    "note": THEME.get(hits[0]["kind"], hits[0]["kind"]) if hits else "Good service"})
    return out


def _code(name: str) -> str:
    parts = name.split()
    return (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")).upper()


# ---------- the digest ----------
def digest(snap: dict, incidents: list, to: str, now: datetime, tz: str) -> bytes:
    local = _local(now, tz)
    alerts = [a for a in snap.get("alerts", []) if a.get("severity") in ("warn", "crit")]
    crit = [a for a in alerts if a["severity"] == "crit"]
    if crit:
        color, title = CRIT, f"{len(crit)} danger alert{'s' if len(crit) != 1 else ''}"
    elif alerts:
        color, title = WARN, f"{len(alerts)} caution alert{'s' if len(alerts) != 1 else ''}"
    else:
        color, title = GOOD, "All systems operational"
    sub = f"{local:%A, %B} {local.day} · morning report"
    parts = [_sign(), _banner(color, title, sub)]
    text = [f"HAHBAH · {title}", sub, ""]

    # Plex backups
    b = snap.get("backup") or {}
    ok = b.get("last_success") or {}
    nights = b.get("nights") or []
    strip = "".join(
        f'<td title="{e(n["day"])}" style="width:{100 / max(1, len(nights)):.2f}%;height:18px;background:'
        f'{ {"ok": GOOD, "failed": CRIT}.get(n["status"], "#CFC9BA") };border:2px solid #fff;border-radius:4px"></td>' for n in nights)
    good_n = sum(1 for n in nights if n["status"] == "ok")
    last = _parse(ok.get("finished_at"))
    pairs = [("Last good backup", f"{_clock(_local(last, tz))} · {ok.get('size_gb', '?')} GB · {ok.get('duration_min', '?')} min" if last else "none recorded"),
             ("Last 14 nights", f"{good_n} of {len(nights)} OK" if nights else "no history yet")]
    r30 = b.get("rate_30d")
    if r30:
        pairs.append(("Last 30 days", f"{r30['good']} of {r30['total']} nights OK"))
    if ok.get("changed_gb") is not None:
        pairs.append(("Sent last night", f"{ok['changed_gb']} GB" + (f" · {ok['files']:,} files" if ok.get("files") is not None else "")))
    if ok.get("db_safe") is not None:
        pairs.append(("Plex database copy", "verified safe (consistent snapshot)" if ok["db_safe"] else "NOT safe: copied while Plex was live"))
    snaps = b.get("snapshots") or {}
    if snaps.get("count"):
        pairs.append(("Weekly snapshots", f"{snaps['count']} kept · newest {snaps.get('newest', '?')}"))
    if b.get("nas_reachable") is not None:
        pairs.append(("Backup target (NAS)", "reachable" if b["nas_reachable"] else "NOT reachable"))
    strip_html = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 10px"><tr>{strip}</tr></table>'
                  if nights else "")
    parts.append(_section("Commuter Rail · backups", LINE_COLORS["purple"], "Plex backups", strip_html + _rows(pairs)))
    text += ["PLEX BACKUPS"] + [f"  {k}: {v}" for k, v in pairs] + [""]

    # Lines
    rows = ""
    for ln in line_statuses(snap):
        c = {"crit": CRIT, "warn": WARN}.get(ln["status"], GOOD)
        rows += (f'<tr><td width="44" style="padding:7px 0;border-top:1px solid #EEE9DD">{_roundel(LINE_COLORS.get(ln["id"], "#7A838C"), _code(ln["name"]))}</td>'
                 f'<td style="padding:7px 0;border-top:1px solid #EEE9DD;font:800 14px/1.3 {FONT};color:{INK}">{e(ln["name"])}'
                 f'<div style="font:600 12px/1.3 {FONT};color:{MUTED}">{e(ln["what"])}</div></td>'
                 f'<td align="right" style="padding:7px 0;border-top:1px solid #EEE9DD;font:800 13px/1.3 {FONT};color:{c}">{e(ln["note"])}</td></tr>')
        text.append(f"  {ln['name']}: {ln['note']}")
    parts.append(_section("The lines", LINE_COLORS["green"], "System health",
                          f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'))

    # Numbers
    m = snap.get("metrics") or {}
    edge = snap.get("edge") or {}
    nums = []
    if m.get("speed_down") is not None:
        nums.append(("Internet", f"{m['speed_down']:.0f} down / {m.get('speed_up', 0):.0f} up Mbps"))
    if m.get("nas_used_pct") is not None:
        nums.append(("NAS volume", f"{m['nas_used_pct']:.0f}% used" + (f" · {m['nas_temp_max']:.0f} °C" if m.get("nas_temp_max") else "")))
    if m.get("drives_ok") is not None:
        nums.append(("NAS drives", f"{int(m['drives_ok'])} of {int(m.get('drives_total', 4))} healthy"))
    if m.get("cert_days") is not None:
        nums.append(("HTTPS certificate", f"{int(m['cert_days'])} days left"))
    dom = _parse((edge.get("domain") or {}).get("expires"))
    if dom:
        nums.append(("hahbah.com", f"{(dom - now).days} days left · auto-renew {'on' if edge['domain'].get('auto_renew') else 'OFF'}"))
    if nums:
        parts.append(_section("Numbers", LINE_COLORS["blue"], "This morning", _rows(nums)))
        text += ["NUMBERS"] + [f"  {k}: {v}" for k, v in nums] + [""]

    # Incidents in the last 24 h
    if incidents:
        items = ""
        for i in incidents:
            o = _local(i["opened_at"], tz)
            end = f"{_clock(_local(i['closed_at'], tz))}" if i.get("closed_at") else "still open"
            if i.get("acked_by"):
                end += f" · acknowledged by {i['acked_by']}"
            if i.get("maint"):
                end += " · in maintenance"
            items += (f'<tr><td style="padding:7px 0;border-top:1px solid #EEE9DD;font:800 13px/1.3 {FONT};color:{SEV_COLOR.get(i["severity"], WARN)};width:30%">'
                      f'{e(SEV_WORD.get(i["severity"], "Caution"))} · {e(THEME.get(i["kind"], i["kind"]))}</td>'
                      f'<td style="padding:7px 0;border-top:1px solid #EEE9DD;font:600 13px/1.4 {FONT};color:{INK}">{e(i["message"])}'
                      f'<div style="font:600 12px/1.3 {MONO};color:{MUTED}">{_clock(o)} → {e(end)}</div></td></tr>')
            text.append(f"  {SEV_WORD.get(i['severity'], 'Caution')}: {i['message']} ({_clock(o)} -> {end})")
        body = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{items}</table>'
    else:
        body = f'<div style="font:600 14px/1.5 {FONT};color:{MUTED}">None. A quiet day on the network.</div>'
        text.append("  No incidents in the last 24 hours.")
    parts.insert(len(parts), _section("Last 24 hours", LINE_COLORS["red"], "Incidents", body))

    subject = f"HAHBAH · {title} · {local:%a %b} {local.day}"
    return _message(to, subject, "\n".join(text) + "\n\nhttps://home.hahbah.com\n", _page("".join(parts), f"{title}. {pairs[0][1]}."))


# ---------- instant alerts ----------
def alert(inc: dict, to: str, now: datetime, tz: str, cleared: bool = False) -> bytes:
    name = THEME.get(inc["kind"], inc["kind"])
    if cleared:
        dur = int(((now - inc["opened_at"]).total_seconds() + 59) // 60)
        title, color = f"All clear · {name}", GOOD
        sub = f"Back to normal after {dur} min · {_clock(_local(now, tz))}"
        lead = f"Resolved: {inc['message']}"
    else:
        title, color = f"Danger · {name}", CRIT
        sub = f"Since {_clock(_local(inc['opened_at'], tz))}"
        lead = inc["message"]
    inner = _sign(small=True) + _banner(color, title, sub) + \
        f'<div style="font:600 16px/1.5 {FONT};color:{INK};padding:6px 4px">{e(lead)}</div>'
    return _message(to, f"HAHBAH {title}", f"{title}\n{sub}\n\n{lead}\n\nhttps://home.hahbah.com\n", _page(inner, lead))


def test_message(to: str, now: datetime, tz: str) -> bytes:
    note = "If you can read this, alerts and the morning digest will reach you."
    inner = (_sign(small=True) + _banner(GOOD, "Test email", f"Sent from Settings · {_clock(_local(now, tz))}")
             + f'<div style="font:600 16px/1.5 {FONT};color:{INK};padding:6px 4px">{note}</div>')
    return _message(to, "HAHBAH test email", note + "\n", _page(inner, "Test email"))


def recipient_welcome(to: str, now: datetime, tz: str) -> bytes:
    note = "You'll now get HAHBAH alerts and the morning report. Ask the admin to remove you."
    inner = (_sign(small=True) + _banner(GOOD, "Welcome aboard", f"Added to HAHBAH email · {_clock(_local(now, tz))}")
             + f'<div style="font:600 16px/1.5 {FONT};color:{INK};padding:6px 4px">{e(note)}</div>')
    return _message(to, "HAHBAH: you're on the list", note + "\n", _page(inner, note))


def recipient_notice(to: str, addr: str, by: str, added: bool, now: datetime, tz: str) -> bytes:
    note = f"{addr} was {'added to' if added else 'removed from'} HAHBAH email by {by}"
    inner = (_sign(small=True) + _banner(GOOD if added else CRIT, "Email list changed", _clock(_local(now, tz)))
             + f'<div style="font:600 16px/1.5 {FONT};color:{INK};padding:6px 4px">{e(note)}</div>'
             + f'<div style="font:600 13px/1.5 {FONT};color:{MUTED};padding:6px 4px">Not you? Sign that account out in Settings.</div>')
    return _message(to, f"HAHBAH email list: {addr} {'added' if added else 'removed'}", note + "\n", _page(inner, note))
