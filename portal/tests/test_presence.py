"""Presence from eero.json: arrivals, the departure debounce, unknown when the Eero data is old, validation. Files only, no network."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import presence

T0 = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)
ALEX_PHONE, SAM_PHONE, TV = "a1a1a1a1a1", "b2b2b2b2b2", "c3c3c3c3c3"
NAMES = {ALEX_PHONE: "Alex phone", SAM_PHONE: "Sam phone", TV: "Living room TV"}
CFG = {"people": [{"id": "alex", "name": "Alex", "devices": [ALEX_PHONE]}, {"id": "sam", "name": "Sam", "devices": [SAM_PHONE]}],
       "away_after_min": 10}


def write_eero(out, at, ids, ok=True):
    out.mkdir(parents=True, exist_ok=True)
    clients = [{"id": i, "name": NAMES[i], "band": "5 GHz", "signal_dbm": "-52 dBm", "wired": False, "ip": "192.168.1.20"} for i in ids]
    doc = {"checked_at": presence.iso(at), "interval_s": 300, "ok": ok, **({"data": {"clients": clients}} if ok else {"error": "HTTPError"})}
    (out / "eero.json").write_text(json.dumps(doc))


def step(out, st, now, cfg=CFG):
    clients, at = presence.read_eero(out, now)
    return presence.update(st, cfg, clients, at, now)


def home(st):
    return {p["id"]: p["home"] for p in st["people"]}


def test_arrival_is_immediate_and_departure_waits_for_the_debounce(tmp_path):
    write_eero(tmp_path, T0, [ALEX_PHONE, TV])
    st, changed = step(tmp_path, None, T0)
    assert changed and home(st) == {"alex": True, "sam": False} and st["history"] == [] and not st["stale"]
    write_eero(tmp_path, T0 + timedelta(minutes=5), [TV, SAM_PHONE])   # Alex's phone naps, Sam arrives
    st, _ = step(tmp_path, st, T0 + timedelta(minutes=5))
    assert home(st) == {"alex": True, "sam": True} and st["history"][-1][1:] == ["sam", "arrive"]
    write_eero(tmp_path, T0 + timedelta(minutes=9, seconds=59), [TV, SAM_PHONE])
    st, _ = step(tmp_path, st, T0 + timedelta(minutes=12))
    assert home(st)["alex"] is True   # missing from the Eero list for 9 min 59 s: still home
    write_eero(tmp_path, T0 + timedelta(minutes=10), [TV, SAM_PHONE])
    st, changed = step(tmp_path, st, T0 + timedelta(minutes=10))
    assert changed and home(st)["alex"] is False and st["history"][-1] == [presence.iso(T0 + timedelta(minutes=10)), "alex", "leave"]
    since = next(p["since"] for p in st["people"] if p["id"] == "alex")
    assert since == presence.iso(T0 + timedelta(minutes=10))


def test_nothing_changes_means_no_write(tmp_path):
    write_eero(tmp_path, T0, [ALEX_PHONE])
    st, _ = step(tmp_path, None, T0)
    assert step(tmp_path, st, T0 + timedelta(seconds=30))[1] is False


@pytest.mark.parametrize("break_it", ["old", "failed", "missing"])
def test_old_or_failed_eero_data_freezes_presence(tmp_path, break_it):
    write_eero(tmp_path, T0, [ALEX_PHONE])
    st, _ = step(tmp_path, None, T0)
    later = T0 + timedelta(minutes=40)
    if break_it == "failed":
        write_eero(tmp_path, later, [], ok=False)
    elif break_it == "missing":
        (tmp_path / "eero.json").unlink()
    st, _ = step(tmp_path, st, later)
    assert st["stale"] and home(st) == {"alex": True, "sam": False} and st["history"] == []   # nobody "left" because the Eero went quiet


def test_devices_seen_are_listed_and_old_ones_pruned(tmp_path):
    write_eero(tmp_path, T0, [TV, ALEX_PHONE])
    st, _ = step(tmp_path, None, T0)
    assert set(st["devices"]) == {TV, ALEX_PHONE} and st["devices"][TV]["name"] == "Living room TV"
    far = T0 + timedelta(days=31)
    write_eero(tmp_path, far, [])
    st, _ = step(tmp_path, st, far)
    assert set(st["devices"]) == {ALEX_PHONE}   # tracked devices are kept; the TV was not seen for 31 days


def test_public_view_hides_devices_from_non_admins(tmp_path):
    write_eero(tmp_path, T0, [TV, ALEX_PHONE])
    st, _ = step(tmp_path, None, T0)
    assert "devices" not in presence.public(st, admin=False)
    rows = {d["id"]: d for d in presence.public(st, admin=True)["devices"]}
    assert rows[ALEX_PHONE]["person"] == "alex" and rows[ALEX_PHONE]["connected"] and rows[TV]["person"] is None
    assert "192.168" not in json.dumps(presence.public(st, admin=True))   # the IP stays in eero.json


@pytest.mark.parametrize("doc, msg", [
    ({"people": [{"id": "a", "name": "A", "devices": [ALEX_PHONE]}, {"id": "b", "name": "B", "devices": [ALEX_PHONE]}]}, "one person"),
    ({"people": [{"id": "A B", "name": "A", "devices": []}]}, "short id"),
    ({"people": [{"id": "a", "name": "", "devices": []}]}, "name"),
    ({"people": [{"id": "a", "name": "A", "devices": ["aa:bb:cc:dd:ee:ff"]}]}, "short ids"),
    ({"people": [], "away_after_min": 0}, "Away after"),
    ({"people": [{"id": f"p{i}", "name": "P", "devices": []} for i in range(13)]}, "At most 12"),
    ({"people": [], "extra": 1}, "Send people"),
])
def test_bad_people_files_are_refused(doc, msg):
    with pytest.raises(ValueError, match=msg):
        presence.validate_people(doc)


def test_read_people_ignores_a_broken_file(tmp_path):
    (tmp_path / "people.json").write_text("{nope")
    assert presence.read_people(tmp_path / "people.json") == {"people": [], "away_after_min": 10}
