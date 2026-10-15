def test_healthz_is_open_and_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_database_file_uses_wal(settings, client):
    import sqlite3
    con = sqlite3.connect(settings.data_dir / "portal.db")
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
