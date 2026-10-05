import json
from dataclasses import replace
from email import message_from_bytes

from app import settingsstore
from app.mailer import MAX_FILE, allowlist, read_web_recipients, send_outbox
from app.main import create_app
from conftest import FakeBuilder
from test_settings_page import SNAP, client

OWNER = "alex@example.com"


def eml(*headers):
    return "\r\n".join(headers).encode() + b"\r\n\r\nhi"


# ----- collector side: the file is untrusted -----
def test_file_is_read_and_unioned_with_owners(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"addresses": ["Pal@Example.com", OWNER, "pal@example.com"]}))
    assert read_web_recipients(f) == ("pal@example.com", OWNER)
    assert allowlist(OWNER, f) == (OWNER, "pal@example.com")   # owners first, no duplicate


def test_missing_or_broken_file_adds_nothing(tmp_path):
    f = tmp_path / "r.json"
    assert allowlist(OWNER, f) == (OWNER,) and allowlist(OWNER) == (OWNER,)
    for body in ("not json", "[]", '{"addresses": "a@b.com"}', '{"addresses": [1, null, {"a": 1}]}'):
        f.write_text(body)
        assert allowlist(OWNER, f) == (OWNER,)


def test_malicious_file_cannot_widen_the_list_past_five_valid_addresses(tmp_path):
    f = tmp_path / "r.json"
    bad = ["a@b.com\nBcc: x@y.com", "a@b.com\r\nBcc: x@y.com", "no-at", "a@b", "a b@c.com", "<a@b.com>", "a@b.com,c@d.com",
           "x@y.com\n", "A" * 300 + "@b.com", "a@b.c", "é@b.com"]
    many = [f"u{i}@example.com" for i in range(50)]
    f.write_text(json.dumps({"addresses": bad + many}))
    got = read_web_recipients(f)
    assert got == tuple(many[:5]) and len(allowlist(OWNER, f)) == 6
    assert not any("\n" in a or "," in a for a in allowlist(OWNER, f))


def test_huge_file_is_ignored(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"addresses": ["ok@example.com"], "pad": "x" * MAX_FILE}))
    assert read_web_recipients(f) == ()


def test_injection_in_pick_header_cannot_reach_a_stranger(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"addresses": ["pal@example.com", "evil@x.com\nBcc: x@y.com"]}))
    ob = tmp_path / "ob"
    ob.mkdir()
    (ob / "a-1.eml").write_bytes(eml("X-HAHBAH-To: pal@example.com, stranger@evil.example, x@y.com", "Subject: S"))
    sent = []
    send_outbox(ob, OWNER, send=lambda raw, to: sent.append(to) or True, recipients_file=f)
    assert sent == ["pal@example.com"]


def test_file_is_re_read_each_cycle(tmp_path):
    f, ob = tmp_path / "r.json", tmp_path / "ob"
    ob.mkdir()
    sent = []
    send = lambda raw, to: sent.append(to) or True
    (ob / "a-1.eml").write_bytes(eml("X-HAHBAH-To: pal@example.com"))
    assert send_outbox(ob, OWNER, send=send, recipients_file=f)["failed"] == 1   # not on the list yet
    f.write_text(json.dumps({"addresses": ["pal@example.com"]}))
    (ob / "a-2.eml").write_bytes(eml("X-HAHBAH-To: pal@example.com"))
    assert send_outbox(ob, OWNER, send=send, recipients_file=f)["sent"] == 1 and sent == ["pal@example.com"]


# ----- portal side -----
def wapp(settings, tmp_path, owners=OWNER):
    ob, md = tmp_path / "outbox", tmp_path / "mail"
    ob.mkdir(exist_ok=True)
    md.mkdir(exist_ok=True)
    return create_app(replace(settings, outbox_dir=ob, mail_dir=md, alert_to=owners), builder=FakeBuilder(SNAP), collect=False)


def post(c, **data):
    return c.post("/settings", data=data, follow_redirects=False, headers={"origin": "https://home.hahbah.com"})


def queued(tmp_path):
    out = []
    for p in sorted((tmp_path / "outbox").glob("*.eml"), key=lambda p: p.name):
        m = message_from_bytes(p.read_bytes())
        out.append((m["X-HAHBAH-To"], m["Subject"]))
    return out


def test_add_writes_file_then_queues_welcome_and_owner_notice(settings, tmp_path):
    app = wapp(settings, tmp_path)
    c = client(app)
    r = post(c, action="add_address", address=" Pal@Example.com ")
    assert r.headers["location"] == "/settings?saved=address-added"
    assert json.loads((tmp_path / "mail" / "recipients.json").read_text()) == {"addresses": ["pal@example.com"], "owners_off": [], "primary": ""}
    assert not list((tmp_path / "mail").glob("*.tmp"))
    q = queued(tmp_path)
    assert [t for t, _ in q] == ["pal@example.com", OWNER]
    assert "on the list" in q[0][1] and "pal@example.com" in q[1][1]
    page = c.get("/settings").text
    assert "pal@example.com" in page and ">owner<" in page
    assert app.state.recorder.recipients == (OWNER, "pal@example.com")


def test_notices_ignore_the_switches_but_stay_on_the_allowlist(settings, tmp_path):
    app = wapp(settings, tmp_path)
    c = client(app)
    post(c, action="save", recipients_form="1", digest_enabled="1", alerts_enabled="1")   # owner switched off for both
    post(c, action="add_address", address="pal@example.com")
    assert [t for t, _ in queued(tmp_path)] == ["pal@example.com", OWNER]


def test_validation_limit_and_duplicates(settings, tmp_path):
    c = client(wapp(settings, tmp_path))
    for bad in ("nope", "a@b", "x@y.com\nBcc: z@y.com", "a" * 250 + "@example.com"):
        assert post(c, action="add_address", address=bad).headers["location"].endswith("address-invalid")
    assert post(c, action="add_address", address=OWNER.upper()).headers["location"].endswith("address-dup")
    for i in range(5):
        assert post(c, action="add_address", address=f"u{i}@example.com").headers["location"].endswith("address-added")
    assert post(c, action="add_address", address="u9@example.com").headers["location"].endswith("address-limit")
    assert post(c, action="add_address", address="u0@example.com").headers["location"].endswith("address-dup")


def test_remove_drops_switches_and_notifies_owners_only(settings, tmp_path):
    app = wapp(settings, tmp_path)
    c = client(app)
    post(c, action="add_address", address="pal@example.com")
    post(c, action="save", recipients_form="1", digest_enabled="1", alerts_enabled="1", alert_to=[OWNER], report_to=[OWNER])
    for p in (tmp_path / "outbox").glob("*.eml"):
        p.unlink()
    assert post(c, action="remove_address", address="pal@example.com").headers["location"].endswith("address-removed")
    assert json.loads((tmp_path / "mail" / "recipients.json").read_text()) == {"addresses": [], "owners_off": [], "primary": ""}
    assert [t for t, _ in queued(tmp_path)] == [OWNER]
    with app.state.SessionLocal() as db:
        assert "pal@example.com" not in settingsstore.get(db, "alerts_off") + settingsstore.get(db, "report_off")
    assert post(c, action="remove_address", address=OWNER).headers["location"].endswith("address-last")   # the only address left can't be removed
    assert app.state.recorder.recipients == (OWNER,)


def test_feature_is_off_without_a_mail_dir(settings, tmp_path):
    ob = tmp_path / "outbox"
    ob.mkdir()
    c = client(create_app(replace(settings, outbox_dir=ob, alert_to=OWNER), builder=FakeBuilder(SNAP), collect=False))
    assert post(c, action="add_address", address="pal@example.com").headers["location"].endswith("address-off")
    assert "Add address" not in c.get("/settings").text


def test_web_addresses_get_alerts_and_follow_the_switches(settings, tmp_path):
    app = wapp(settings, tmp_path)
    c = client(app)
    post(c, action="add_address", address="pal@example.com")
    rec = app.state.recorder
    assert rec.picks("danger") == (OWNER, "pal@example.com")
    post(c, action="save", recipients_form="1", digest_enabled="1", alerts_enabled="1", alert_to=[OWNER])
    assert rec.picks("danger") == (OWNER,) and rec.picks("digest") == ()


# ----- owners can be removed and the primary changed from Settings -----
def recips(app):
    return app.state.recorder.recipients


def test_primary_can_be_changed_and_the_collector_agrees(settings, tmp_path):
    app = wapp(settings, tmp_path)
    c = client(app)
    post(c, action="add_address", address="pal@example.com")
    assert post(c, action="make_primary", address="pal@example.com").headers["location"].endswith("primary-set")
    assert recips(app) == ("pal@example.com", OWNER)
    assert allowlist(OWNER, tmp_path / "mail" / "recipients.json") == ("pal@example.com", OWNER)
    assert post(c, action="make_primary", address="nobody@example.com").headers["location"].endswith("address-gone")


def test_an_owner_address_can_be_removed_and_comes_back_if_added_again(settings, tmp_path):
    app = wapp(settings, tmp_path)
    c = client(app)
    post(c, action="add_address", address="pal@example.com")
    assert post(c, action="remove_address", address=OWNER).headers["location"].endswith("address-removed")
    assert recips(app) == ("pal@example.com",)
    assert allowlist(OWNER, tmp_path / "mail" / "recipients.json") == ("pal@example.com",)
    assert post(c, action="add_address", address=OWNER).headers["location"].endswith("address-added")
    assert set(recips(app)) == {OWNER, "pal@example.com"}


def test_the_last_address_cannot_be_removed(settings, tmp_path):
    app = wapp(settings, tmp_path)
    c = client(app)
    assert post(c, action="remove_address", address=OWNER).headers["location"].endswith("address-last")
    assert recips(app) == (OWNER,)


def test_removing_the_primary_promotes_the_next_one(settings, tmp_path):
    app = wapp(settings, tmp_path)
    c = client(app)
    post(c, action="add_address", address="pal@example.com")
    post(c, action="make_primary", address="pal@example.com")
    post(c, action="remove_address", address="pal@example.com")
    assert recips(app) == (OWNER,)


def test_settings_page_offers_remove_and_make_primary_for_every_row(settings, tmp_path):
    app = wapp(settings, tmp_path)
    c = client(app)
    post(c, action="add_address", address="pal@example.com")
    page = c.get("/settings").text
    assert f'aria-label="Remove {OWNER}"' in page and 'aria-label="Remove pal@example.com"' in page
    assert 'aria-label="Make pal@example.com primary"' in page and f'aria-label="Make {OWNER} primary"' not in page


def test_effective_list_never_ends_up_empty_and_ignores_junk():
    from app.mailer import effective
    assert effective(OWNER, (), (OWNER,), "") == (OWNER,)
    assert effective(OWNER, ("pal@example.com",), (OWNER,), "stranger@evil.example") == ("pal@example.com",)
