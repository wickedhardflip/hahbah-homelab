from app.collectors.deploy import last_deploy


def test_last_successful_deploy(tmp_path):
    log = tmp_path / "deploy.log"
    log.write_text("2026-09-30T13:20:19-04:00 deployed adde148 (Eero probe)\n"
                   "2026-09-30T13:42:34-04:00 deployed 2802e07 (Phase 1: Caddy (wildcard))\n")
    assert last_deploy(log) == {"commit": "2802e07", "message": "Phase 1: Caddy (wildcard)",
                                "at": "2026-09-30T13:42:34-04:00", "ok": True}


def test_failed_config_check_is_not_ok(tmp_path):
    log = tmp_path / "deploy.log"
    log.write_text("2026-09-30T14:00:00-04:00 FAILED config check at abc1234 (broken compose)\n")
    d = last_deploy(log)
    assert d["ok"] is False and d["commit"] == "abc1234"


def test_missing_or_empty_log_is_none(tmp_path):
    assert last_deploy(tmp_path / "nope.log") is None
    (tmp_path / "empty.log").write_text("\n")
    assert last_deploy(tmp_path / "empty.log") is None


def test_unreadable_line_is_reported(tmp_path):
    (tmp_path / "d.log").write_text("garbage\n")
    d = last_deploy(tmp_path / "d.log")
    assert d["ok"] is False and "garbage" in d["message"]


def test_non_utf8_byte_in_log_is_tolerated(tmp_path):
    log = tmp_path / "deploy.log"
    log.write_bytes(b"2026-09-30T14:00:00-04:00 deployed abc1234 (caf\xe9 fix)\n")
    d = last_deploy(log)
    assert d["ok"] is True and d["commit"] == "abc1234"


def test_failed_build_and_firewall_lines_are_failures(tmp_path):
    from app.collectors.deploy import last_deploy
    for kind in ("FAILED build at", "FAILED firewall at", "FAILED caddy reload at"):
        p = tmp_path / "deploy.log"
        p.write_text(f"2026-10-01T20:20:00-04:00 deployed abc1234 (ok)\n2026-10-01T20:25:00-04:00 {kind} def5678 (Broken thing)\n")
        d = last_deploy(p)
        assert d == {"commit": "def5678", "message": "Broken thing", "at": "2026-10-01T20:25:00-04:00", "ok": False}


def test_version_is_read_when_present_and_optional(tmp_path):
    log = tmp_path / "deploy.log"
    log.write_text("2026-10-07T10:00:00-04:00 deployed 8f80cbc (old line)\n2026-10-07T11:00:00-04:00 deployed 1a2b3c4 v1.2 (new line)\n")
    got = last_deploy(log, history=True)
    assert got["version"] == "1.2" and got["commit"] == "1a2b3c4"
    assert [h.get("version") for h in got["history"]] == ["1.2", None]
