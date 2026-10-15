import importlib.util
import json
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "hosts" / "central" / "wireguard" / "ddns.py"
SECRET = "pk1_SUPERSECRET"


@pytest.fixture
def ddns(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("ddns_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("DDNS_STATUS", str(tmp_path / "ddns.json"))
    monkeypatch.setenv("PORKBUN_API_KEY", SECRET)
    monkeypatch.setenv("PORKBUN_API_SECRET_KEY", SECRET + "_2")
    monkeypatch.delenv("DDNS_NAME", raising=False)
    mod.calls = []
    return mod


def wire(ddns, monkeypatch, wan, responses):
    monkeypatch.setattr(ddns.upnp, "eero_status", lambda url: {"public_ip": wan})
    it = iter(responses)

    def fake(path, keys, extra=None):
        ddns.calls.append((path, extra))
        return next(it)
    monkeypatch.setattr(ddns, "_call", fake)


def status(tmp_path):
    return json.loads((tmp_path / "ddns.json").read_text())


def rec(ip):
    return {"status": "SUCCESS", "records": [{"content": ip}] if ip else []}


def test_no_change_makes_no_write_call(ddns, monkeypatch, tmp_path):
    wire(ddns, monkeypatch, "1.2.3.4", [rec("1.2.3.4")])
    ddns.run()
    assert len(ddns.calls) == 1 and "retrieve" in ddns.calls[0][0]
    s = status(tmp_path)
    assert s["ok"] and s["dns_ip"] == "1.2.3.4" and s["updated_at"] is None


def test_changed_makes_one_edit(ddns, monkeypatch, tmp_path):
    wire(ddns, monkeypatch, "1.2.3.4", [rec("9.9.9.9"), {"status": "SUCCESS"}])
    ddns.run()
    assert len(ddns.calls) == 2
    assert ddns.calls[1][0] == "/dns/editByNameType/example.com/A/ticket"
    assert ddns.calls[1][1]["content"] == "1.2.3.4"
    s = status(tmp_path)
    assert s["ok"] and s["dns_ip"] == "1.2.3.4" and s["updated_at"]


def test_missing_record_is_created(ddns, monkeypatch, tmp_path):
    wire(ddns, monkeypatch, "1.2.3.4", [rec(None), {"status": "SUCCESS"}])
    ddns.run()
    assert ddns.calls[1][0] == "/dns/create/example.com"
    assert ddns.calls[1][1] == {"name": "ticket", "type": "A", "content": "1.2.3.4", "ttl": "300"}
    assert status(tmp_path)["ok"]


def test_upnp_failure_touches_no_dns(ddns, monkeypatch, tmp_path):
    wire(ddns, monkeypatch, None, [])
    assert ddns.main() == 0
    assert ddns.calls == []
    s = status(tmp_path)
    assert not s["ok"] and s["wan_ip"] is None and "UPnP" in s["error"]


def test_porkbun_error_sets_ok_false_without_secrets(ddns, monkeypatch, tmp_path, capsys):
    wire(ddns, monkeypatch, "1.2.3.4", [{"status": "ERROR", "message": f"bad key {SECRET}"}])
    ddns.main()
    out = capsys.readouterr()
    s = status(tmp_path)
    assert not s["ok"] and s["error"]
    logs = "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert SECRET not in out.out + out.err + (tmp_path / "ddns.json").read_text() + logs


def test_status_file_shape_and_updated_at_preserved(ddns, monkeypatch, tmp_path):
    (tmp_path / "ddns.json").write_text(json.dumps({"updated_at": "2026-01-01T00:00:00+00:00"}))
    wire(ddns, monkeypatch, "1.2.3.4", [rec("1.2.3.4")])
    ddns.run()
    s = status(tmp_path)
    assert set(s) == {"checked_at", "name", "ok", "wan_ip", "dns_ip", "updated_at", "error"}
    assert s["name"] == "ticket.example.com" and s["updated_at"] == "2026-01-01T00:00:00+00:00"
    assert not list(tmp_path.glob(".ddns-*"))


def test_name_comes_from_env(ddns, monkeypatch, tmp_path):
    monkeypatch.setenv("DDNS_NAME", "gate")
    wire(ddns, monkeypatch, "1.2.3.4", [rec("1.2.3.4")])
    ddns.run()
    assert ddns.calls[0][0].endswith("/A/gate") and status(tmp_path)["name"] == "gate.example.com"
