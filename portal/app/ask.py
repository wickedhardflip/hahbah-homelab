"""Ask hubbah: the context builder, the Ollama client and the chat history table. No web code here (see routes_ask.py).

The model is small, so the prompt is the whole picture as compact JSON and the model only reads it: no tool calling, no writes.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import utcnow

SYSTEM = ("You are Hubbah, the assistant for one family's home network and smart plugs. Answer only from the JSON context. "
          "Be brief and friendly. If the context does not say, say you do not know. No dashes as punctuation.")
OPTIONS = {"num_ctx": 8192, "temperature": 0.2}
KEEP_ALIVE = "30m"
HISTORY_TURNS = 6          # earlier messages sent back with a question (3 exchanges)
MAX_QUESTION = 1000
SAMPLES = ["Is anything wrong right now?", "Which plugs are on?", "Who is home?", "What changed in the last day?",
           "How full are the disks?", "Is the internet healthy?"]


class AskMessage(Base):
    """One chat message, owned by one user. A chat is every row sharing a chat_id; a new table, so create_all adds it on startup."""
    __tablename__ = "ask_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    chat_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)   # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class DashStripper:
    """Streams text with em/en dashes turned into commas ("a — b" -> "a, b"). Trailing spaces wait for the next character."""

    def __init__(self):
        self.pending, self.need_space = "", False

    def feed(self, text: str) -> str:
        out = []
        for c in text:
            if c in "—–":
                self.pending, self.need_space = "", True
                out.append(",")
            elif c.isspace():
                self.pending += c
            else:
                if self.need_space and not self.pending:
                    out.append(" ")
                out.append(self.pending + c)
                self.pending, self.need_space = "", False
        return "".join(out)

    def flush(self) -> str:
        out, self.pending = self.pending, ""
        return out


def strip_dashes(text: str) -> str:
    s = DashStripper()
    return s.feed(text) + s.flush()


def _stat(n: dict, key: str):
    v = (n.get("stats") or {}).get(key)
    return round(v, 1) if isinstance(v, float) else v


def build_context(snap: dict, incidents: list, plugs: dict, people: list, now: datetime) -> dict:
    """A compact picture of the house: hosts and apps, open alerts, recent incidents, plugs, who is home."""
    hosts = []
    for n in snap.get("nodes", []):
        row = {"name": n.get("label") or n.get("id"), "status": n.get("status", "unknown")}
        for k in ("cpu_pct", "mem_pct", "temp_c", "volume_used_pct"):
            if _stat(n, k) is not None:
                row[k] = _stat(n, k)
        disks = [f"{d.get('mount')} {d.get('pct')}%" for d in (n.get("stats") or {}).get("disks", []) if isinstance(d, dict)]
        if disks:
            row["disks"] = disks
        hosts.append(row)
    ctx = {"time": now.strftime("%Y-%m-%d %H:%M %a"), "hosts": hosts,
           "open_alerts": [{"severity": a.get("severity"), "message": a.get("message")} for a in snap.get("alerts", [])][:15],
           "recent_incidents": [{"severity": i.get("severity"), "message": i.get("message"), "opened": str(i.get("opened_at"))[:16],
                                 "closed": str(i.get("closed_at") or "")[:16] or None} for i in incidents[:15]]}
    if snap.get("backup"):
        ctx["backup"] = {k: v for k, v in snap["backup"].items() if isinstance(v, (str, int, float, bool))}
    if plugs:
        ctx["plugs"] = [{"name": p["name"], "on": p["on"], "online": p["online"]} for p in plugs.values()]
    if people:
        ctx["people"] = {p["name"]: ("unknown" if p.get("home") is None else "home" if p["home"] else "away") for p in people}
    return ctx


def messages_for(ctx: dict, history: list, question: str) -> list:
    """System prompt, the last few turns, then the question with a fresh context (stale context from old turns is not resent)."""
    msgs = [{"role": "system", "content": SYSTEM}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in history[-HISTORY_TURNS:]]
    msgs.append({"role": "user", "content": "Context JSON:\n" + json.dumps(ctx, separators=(",", ":")) + "\n\nQuestion: " + question})
    return msgs


class OllamaDown(Exception):
    pass


def _get(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


class Ollama:
    """Thin client for the PC's Ollama. Every failure is an OllamaDown with a short reason: the PC sleeps, so that is normal."""

    def __init__(self, url: str, model: str, fallback: str, get=_get, open_stream=None):
        self.url, self.model, self.fallback = url.rstrip("/"), model, fallback
        self._get = get
        self._open = open_stream or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))

    def status(self) -> dict:
        """{state: 'unknown'|'cold'|'ready'|'nomodel', model}. cold = reachable but the model must load (35 to 65 s)."""
        if not self.url:
            return {"state": "unknown", "model": None, "reason": "OLLAMA_URL is not set"}
        try:
            tags = {m["name"] for m in self._get(self.url + "/api/tags", 3).get("models", [])}
            loaded = {m["name"] for m in self._get(self.url + "/api/ps", 3).get("models", [])}
        except Exception as e:  # noqa: BLE001 (asleep, firewalled, odd reply: all "unknown")
            return {"state": "unknown", "model": None, "reason": type(e).__name__}
        model = next((m for m in (self.model, self.fallback) if m in tags), None)
        if model is None:
            return {"state": "nomodel", "model": None, "reason": "Neither model is installed on the PC."}
        return {"state": "ready" if model in loaded else "cold", "model": model}

    def stream(self, model: str, msgs: list):
        """Yields ('thinking', '') and ('token', text) as Ollama produces them. Raises OllamaDown."""
        body = json.dumps({"model": model, "messages": msgs, "stream": True, "keep_alive": KEEP_ALIVE, "options": OPTIONS}).encode()
        req = urllib.request.Request(self.url + "/api/chat", body, {"Content-Type": "application/json"})
        try:
            with self._open(req, 180) as r:   # the first call after idle loads the model: allow for it
                for line in r:
                    if not line.strip():
                        continue
                    msg = json.loads(line).get("message") or {}
                    if msg.get("content"):
                        yield "token", msg["content"]
                    elif msg.get("thinking"):
                        yield "thinking", ""
        except (OSError, ValueError, urllib.error.URLError) as e:
            raise OllamaDown(type(e).__name__) from e
