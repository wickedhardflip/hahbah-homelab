"""Ask hubbah: dash stripping, context shape, model choice, the streaming API (fake Ollama, no network), per-user history, auth."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ask
from app.main import create_app
from app.models import User
from conftest import FakeBuilder, make_user, sign_in

OK = {"origin": "https://home.example.com"}
NOW = datetime(2026, 10, 24, 22, 10, tzinfo=timezone.utc)


def test_dash_stripper_handles_split_tokens_and_spacing():
    s = ask.DashStripper()
    out = "".join(s.feed(t) for t in ["It is fine ", "—", " really", "–ok", " done "]) + s.flush()
    assert out == "It is fine, really, ok done "
    assert ask.strip_dashes("a—b") == "a, b"


def test_context_is_compact_and_has_each_section():
    snap = {"nodes": [{"id": "nas", "label": "NAS", "status": "warn", "stats": {"cpu_pct": 8.123, "disks": [{"mount": "/", "pct": 92}]}}],
            "alerts": [{"severity": "warn", "message": "NAS is 92% full"}], "backup": {"last_good": "2026-10-24", "x": {"nested": 1}}}
    inc = [{"severity": "warn", "message": "NAS is 92% full", "opened_at": "2026-10-22 04:05:00", "closed_at": None}]
    plugs = {"k": {"id": "1", "name": "Hall lamp", "online": True, "on": True}}
    ctx = ask.build_context(snap, inc, plugs, [{"name": "Alex", "home": True}, {"name": "Sam", "home": None}], NOW)
    assert ctx["hosts"] == [{"name": "NAS", "status": "warn", "cpu_pct": 8.1, "disks": ["/ 92%"]}]
    assert ctx["open_alerts"][0]["message"] == "NAS is 92% full" and ctx["recent_incidents"][0]["opened"] == "2026-10-22 04:05"
    assert ctx["backup"] == {"last_good": "2026-10-24"} and ctx["plugs"] == [{"name": "Hall lamp", "on": True, "online": True}]
    assert ctx["people"] == {"Alex": "home", "Sam": "unknown"} and ctx["time"].startswith("2026-10-24 22:10")
    assert len(json.dumps(ctx)) < 1500


def test_messages_keep_only_recent_history_and_a_fresh_context():
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(10)]
    msgs = ask.messages_for({"a": 1}, hist, "Who is home?")
    assert msgs[0]["role"] == "system" and [m["content"] for m in msgs[1:-1]] == ["m4", "m5", "m6", "m7", "m8", "m9"]
    assert msgs[-1]["content"].endswith("Question: Who is home?") and '{"a":1}' in msgs[-1]["content"]


def fake_get(tags, loaded=(), fail=False):
    def get(url, timeout):
        if fail:
            raise OSError("asleep")
        return {"models": [{"name": n} for n in (loaded if url.endswith("/api/ps") else tags)]}
    return get


@pytest.mark.parametrize("tags,loaded,want", [
    (["gpt-oss:20b", "gemma4:12b-it-qat"], ["gpt-oss:20b"], ("ready", "gpt-oss:20b")),
    (["gpt-oss:20b"], [], ("cold", "gpt-oss:20b")),
    (["gemma4:12b-it-qat"], [], ("cold", "gemma4:12b-it-qat")),     # default missing: the fallback, never a pull
    (["llama3:8b"], [], ("nomodel", None)),
])
def test_status_picks_an_installed_model(tags, loaded, want):
    st = ask.Ollama("http://pc:11434", "gpt-oss:20b", "gemma4:12b-it-qat", get=fake_get(tags, loaded)).status()
    assert (st["state"], st["model"]) == want


def test_unreachable_pc_is_unknown_not_an_error():
    assert ask.Ollama("http://pc:11434", "a", "b", get=fake_get([], fail=True)).status()["state"] == "unknown"
    assert ask.Ollama("", "a", "b").status()["state"] == "unknown"


class FakeOllama:
    def __init__(self, state="cold", chunks=("The hall ", "lamp is on ", "— and ", "cozy."), down=False):
        self.state, self.chunks, self.down, self.sent = state, chunks, down, None

    def status(self):
        return {"state": self.state, "model": "gpt-oss:20b", "reason": "x"}

    def stream(self, model, msgs):
        self.sent = msgs
        yield "thinking", ""
        for c in self.chunks:
            yield "token", c
        if self.down:
            raise ask.OllamaDown("ReadTimeout")


@pytest.fixture
def app(settings):
    a = create_app(settings, builder=FakeBuilder(), collect=False)
    a.state.ollama = FakeOllama()
    return a


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def admin(app, client, name="alex"):
    make_user(app, name)
    with app.state.SessionLocal() as db:
        db.query(User).filter_by(username=name).update({"is_admin": True})
        db.commit()
    sign_in(client, name)


def ask_q(client, q="Is the hall lamp on?", **kw):
    r = client.post("/api/ask", json={"question": q, **kw}, headers=OK)
    return r, [json.loads(x) for x in r.text.splitlines()]


def test_answer_streams_strips_dashes_and_saves_the_chat(app, client):
    admin(app, client)
    r, lines = ask_q(client)
    assert r.status_code == 200 and {"state": "thinking"} in lines
    assert "".join(m.get("token", "") for m in lines) == "The hall lamp is on, and cozy."
    cid = lines[-1]["chat_id"]
    chat = client.get(f"/api/ask/chats/{cid}").json()
    assert chat["messages"] == [{"role": "user", "content": "Is the hall lamp on?"}, {"role": "assistant", "content": "The hall lamp is on, and cozy."}]
    assert client.get("/api/ask/chats").json()["chats"][0]["title"] == "Is the hall lamp on?"
    ask_q(client, "And the tree?", chat_id=cid)   # a follow up carries the earlier turns
    sent = app.state.ollama.sent
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"] and "Context JSON" in sent[-1]["content"]


def test_sleeping_pc_is_a_calm_unknown_and_saves_nothing(app, client):
    admin(app, client)
    app.state.ollama = FakeOllama(state="unknown")
    _, lines = ask_q(client)
    assert lines == [{"error": "The home PC isn't answering, so it may be asleep. Try again in a minute, or wake it first.", "state": "unknown"}]
    assert client.get("/api/ask/chats").json() == {"chats": []}


def test_pc_dropping_mid_answer_reports_unknown_and_saves_nothing(app, client):
    admin(app, client)
    app.state.ollama = FakeOllama(down=True)
    _, lines = ask_q(client)
    assert lines[-1]["state"] == "unknown" and client.get("/api/ask/chats").json() == {"chats": []}


def test_chats_are_private_per_user_and_deletable(app, client):
    admin(app, client)
    cid = ask_q(client)[1][-1]["chat_id"]
    client.post("/logout")
    admin(app, client, "other")
    assert client.get(f"/api/ask/chats/{cid}").status_code == 404
    assert client.delete(f"/api/ask/chats/{cid}", headers=OK).status_code == 404
    client.post("/logout")
    sign_in(client, "alex")
    assert client.delete(f"/api/ask/chats/{cid}", headers=OK).status_code == 200
    assert client.get("/api/ask/chats").json() == {"chats": []}


def test_validation_origin_and_admin_only(app, client):
    admin(app, client)
    assert client.post("/api/ask", json={"question": "hi"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/ask", json={"question": "  "}, headers=OK).status_code == 422
    assert client.post("/api/ask", json={"question": "x" * 1001}, headers=OK).status_code == 422
    assert client.post("/api/ask", json={"question": "hi", "chat_id": "../x"}, headers=OK).status_code == 422
    assert client.post("/api/ask", content="nope", headers=OK).status_code == 422
    client.post("/logout")
    make_user(app, "guest", "another long password")
    sign_in(client, "guest", "another long password")
    assert client.get("/ask").status_code == 403 and client.post("/api/ask", json={"question": "hi"}, headers=OK).status_code == 403


def test_page_renders_for_admin_with_samples(app, client):
    admin(app, client)
    r = client.get("/ask")
    assert r.status_code == 200 and "Is anything wrong right now?" in r.text and "/static/ask.js" in r.text
    assert client.get("/api/ask/status").json()["state"] == "cold"


def test_ask_assets_use_no_innerhtml():
    static = Path(__file__).parent.parent / "app" / "static"
    assert "innerHTML" not in (static / "ask.js").read_text(encoding="utf-8")
