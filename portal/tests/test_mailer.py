import os
import time

from app.mailer import send_outbox

CRLF = b"\r\n"


def msg(*headers, body=b"hi"):
    return CRLF.join(h.encode() for h in headers) + CRLF + CRLF + body


def test_sends_and_deletes_each_message(tmp_path):
    (tmp_path / "a.eml").write_bytes(msg("To: x@example.com", "Subject: A"))
    (tmp_path / "b.eml").write_bytes(msg("To: x@example.com", "Subject: B"))
    sent = []
    r = send_outbox(tmp_path, to="me@example.com", send=lambda raw, to: sent.append(raw) or True)
    assert r == {"sent": 2, "queued": 0, "failed": 0} and len(sent) == 2 and not list(tmp_path.glob("*.eml"))


def test_failures_stay_queued_then_move_to_failed_after_a_day(tmp_path):
    m = tmp_path / "c.eml"
    m.write_bytes(msg("To: x@example.com", "Subject: C"))
    assert send_outbox(tmp_path, to="me@example.com", send=lambda raw, to: False) == {"sent": 0, "queued": 1, "failed": 0}
    old = time.time() - 25 * 3600
    os.utime(m, (old, old))
    assert send_outbox(tmp_path, to="me@example.com", send=lambda raw, to: False) == {"sent": 0, "queued": 0, "failed": 1}
    assert (tmp_path / "failed" / "c.eml").exists()


def test_temp_files_being_written_are_ignored(tmp_path):
    (tmp_path / "d.eml.tmp").write_bytes(b"partial")
    assert send_outbox(tmp_path, to="me@example.com", send=lambda raw, to: True) == {"sent": 0, "queued": 0, "failed": 0}


def test_recipients_come_from_the_collector_not_the_message(tmp_path):
    (tmp_path / "x-1.eml").write_bytes(msg("To: stranger@example.com", "Cc: a@example.com", "Bcc: b@example.com",
                                           "Subject: Hi", body=b"body"))
    got = []
    send_outbox(tmp_path, to="me@example.com", send=lambda raw, to: got.append((raw, to)) or True)
    raw, to = got[0]
    assert to == "me@example.com"
    assert b"stranger@" not in raw and b"a@example.com" not in raw and b"b@example.com" not in raw
    assert b"To: me@example.com" in raw and b"Subject: Hi" in raw


def test_oversized_messages_are_never_sent(tmp_path):
    (tmp_path / "big-1.eml").write_bytes(msg("Subject: Big", body=b"x" * 3_000_000))
    r = send_outbox(tmp_path, to="me@example.com", send=lambda raw, to: True)
    assert r["sent"] == 0 and r["failed"] == 1 and (tmp_path / "failed" / "big-1.eml").exists()


def test_nothing_is_sent_without_a_configured_recipient(tmp_path):
    (tmp_path / "x-1.eml").write_bytes(msg("Subject: Hi"))
    assert send_outbox(tmp_path, to="", send=lambda raw, to: True) == {"sent": 0, "queued": 1, "failed": 0}


def test_sent_oldest_first(tmp_path):
    (tmp_path / "digest-300.eml").write_bytes(msg("Subject: 3"))
    (tmp_path / "clear-200.eml").write_bytes(msg("Subject: 2"))
    (tmp_path / "danger-100.eml").write_bytes(msg("Subject: 1"))
    got = []
    send_outbox(tmp_path, to="me@example.com", send=lambda raw, to: got.append(raw) or True)
    assert [g.split(b"Subject: ")[1][:1] for g in got] == [b"1", b"2", b"3"]


def test_allowlist_parsing():
    from app.mailer import parse_recipients
    assert parse_recipients(" A@x.com, b@y.com ,a@x.com,,junk") == ("a@x.com", "b@y.com")


def test_each_picked_address_gets_its_own_copy(tmp_path):
    (tmp_path / "danger-1.eml").write_bytes(msg("X-HAHBAH-To: b@example.com, me@example.com", "Subject: D"))
    got = []
    r = send_outbox(tmp_path, to="me@example.com,b@example.com", send=lambda raw, to: got.append((raw, to)) or True)
    assert r["sent"] == 1 and [t for _, t in got] == ["me@example.com", "b@example.com"]
    for raw, to in got:   # nobody sees the other address, and the pick header never leaves the server
        other = b"b@example.com" if to == "me@example.com" else b"me@example.com"
        assert other not in raw and b"X-HAHBAH-To" not in raw


def test_the_portal_cannot_pick_addresses_off_the_allowlist(tmp_path):
    (tmp_path / "danger-1.eml").write_bytes(msg("X-HAHBAH-To: stranger@evil.example, me@example.com", "Subject: D"))
    (tmp_path / "danger-2.eml").write_bytes(msg("X-HAHBAH-To: stranger@evil.example", "Subject: E"))
    got = []
    r = send_outbox(tmp_path, to="me@example.com", send=lambda raw, to: got.append(to) or True)
    assert got == ["me@example.com"] and r == {"sent": 1, "queued": 0, "failed": 1}


def test_no_pick_header_means_the_primary_only(tmp_path):
    (tmp_path / "x-1.eml").write_bytes(msg("Subject: Old"))
    got = []
    send_outbox(tmp_path, to="me@example.com,b@example.com", send=lambda raw, to: got.append(to) or True)
    assert got == ["me@example.com"]


def test_a_partial_send_retries_only_the_addresses_that_failed(tmp_path):
    m = tmp_path / "danger-1.eml"
    m.write_bytes(msg("X-HAHBAH-To: me@example.com, b@example.com", "Subject: D"))
    old = time.time() - 3600
    os.utime(m, (old, old))
    got = []
    r = send_outbox(tmp_path, to="me@example.com,b@example.com", send=lambda raw, to: got.append(to) or to == "me@example.com")
    assert r["queued"] == 1 and abs(m.stat().st_mtime - old) < 2   # keeps its age, so it still gives up after a day
    got.clear()
    assert send_outbox(tmp_path, to="me@example.com,b@example.com", send=lambda raw, to: got.append(to) or True)["sent"] == 1
    assert got == ["b@example.com"]


def test_outbox_is_recreated_after_deletion(tmp_path):
    from app.mailer import write_message
    box = tmp_path / "gone" / "outbox"
    p = write_message(box, b"Subject: x\n\nhi", "alert")   # portal side: dir missing -> created
    assert p.exists() and p.parent == box
    import shutil
    shutil.rmtree(box)
    assert send_outbox(box, to="me@example.com", send=lambda raw, to: True) == {"sent": 0, "queued": 0, "failed": 0}
    assert box.is_dir()   # collector side recreates it too
    assert write_message(box, b"Subject: y\n\nhi", "alert").exists()
