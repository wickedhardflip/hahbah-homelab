import email
from email import policy
from datetime import datetime, timezone

from app.emails import digest

NOW = datetime(2026, 10, 2, 10, 30, tzinfo=timezone.utc)


def body(snap):
    m = email.message_from_bytes(digest(snap, [], "a@b.c", NOW, "America/New_York"), policy=policy.default)
    return m.get_body(preferencelist=("plain",)).get_content()


def test_digest_shows_backup_detail():
    ok = {"finished_at": "2026-10-02T07:16:44Z", "size_gb": 6.18, "duration_min": 17, "changed_gb": 0.42, "files": 1234, "db_safe": True}
    t = body({"backup": {"last_success": ok, "nights": [{"day": "2026-10-01", "status": "ok"}], "rate_30d": {"good": 28, "total": 30}}})
    assert "Last 30 days: 28 of 30 nights OK" in t and "Sent last night: 0.42 GB · 1,234 files" in t
    assert "Plex database copy: verified safe" in t


def test_digest_flags_unsafe_db_and_skips_unrecorded():
    ok = {"finished_at": "2026-10-02T07:16:44Z", "size_gb": 6.18, "duration_min": 17, "changed_gb": 0.1, "files": None, "db_safe": False}
    assert "NOT safe" in body({"backup": {"last_success": ok}})
    ok.pop("changed_gb"); ok["db_safe"] = None
    t = body({"backup": {"last_success": ok}})
    assert "Sent last night" not in t and "database copy" not in t and "Last 30 days" not in t
