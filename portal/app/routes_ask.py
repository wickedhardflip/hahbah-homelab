"""Ask hubbah chat page and API. Mounted by main.create_app with install(). Admin only for now.

Chats are per user (ask_messages). The answer streams as newline-delimited JSON: {"state": "thinking"}, {"token": "..."},
then {"done": true, "chat_id": "..."} or {"error": "..."}. The PC sleeps, so "can't reach it" is a calm "unknown", never an alert.
"""
import json
import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func

from . import ask
from . import automations as auto
from .auth import current_user, same_origin

DOWN = "The home PC isn't answering, so it may be asleep. Try again in a minute, or wake it first."


def install(app, templates) -> None:
    settings, SessionLocal = app.state.settings, app.state.SessionLocal
    app.state.ollama = ask.Ollama(settings.ollama_url, settings.ollama_model, settings.ollama_fallback)   # tests swap this

    def admin_user(user=Depends(current_user)):
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admins only.")
        return user

    def context(user) -> dict:
        view = app.state.visible_view(user)
        incidents = [] if app.state.is_discreet() else app.state.recorder.recent_incidents(hours=24 * 7, limit=15)
        people = []
        try:
            doc = json.loads((settings.collector_dir / "automations.json").read_text(encoding="utf-8"))
            people = doc["data"]["people"] if not doc["data"].get("stale") else []
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return ask.build_context(view, incidents, auto.plug_rows(settings.collector_dir, settings.plugs_dir), people, datetime.now(timezone.utc))

    @app.get("/ask")
    def ask_page(request: Request, user=Depends(admin_user)):
        return templates.TemplateResponse(request, "ask.html", {"user": user, "is_admin": True, "samples": ask.SAMPLES})

    @app.get("/api/ask/status")
    def ask_status(user=Depends(admin_user)):
        return app.state.ollama.status()

    @app.get("/api/ask/chats")
    def ask_chats(user=Depends(admin_user)):
        with SessionLocal() as db:
            rows = (db.query(ask.AskMessage.chat_id, func.max(ask.AskMessage.created_at), func.min(ask.AskMessage.id))
                    .filter(ask.AskMessage.user_id == user.id).group_by(ask.AskMessage.chat_id)
                    .order_by(func.max(ask.AskMessage.created_at).desc()).limit(40).all())
            out = [{"id": cid, "title": db.get(ask.AskMessage, first).content[:60], "at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}
                   for cid, at, first in rows]
        return {"chats": out}

    @app.get("/api/ask/chats/{cid}")
    def ask_chat(cid: str, user=Depends(admin_user)):
        with SessionLocal() as db:
            rows = db.query(ask.AskMessage).filter_by(user_id=user.id, chat_id=cid).order_by(ask.AskMessage.id).all()
        if not rows:
            raise HTTPException(status_code=404, detail="No such chat.")
        return {"id": cid, "messages": [{"role": m.role, "content": m.content} for m in rows]}

    @app.delete("/api/ask/chats/{cid}")
    def ask_delete(request: Request, cid: str, user=Depends(admin_user)):
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        with SessionLocal() as db:
            n = db.query(ask.AskMessage).filter_by(user_id=user.id, chat_id=cid).delete()
            db.commit()
        if not n:
            raise HTTPException(status_code=404, detail="No such chat.")
        return {"deleted": cid}

    @app.post("/api/ask")
    async def ask_question(request: Request, user=Depends(admin_user)):
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 (not JSON)
            body = None
        body = body if isinstance(body, dict) else {}
        q, cid = body.get("question"), body.get("chat_id")
        if not isinstance(q, str) or not q.strip() or len(q) > ask.MAX_QUESTION:
            raise HTTPException(status_code=422, detail=f"Send a question of 1 to {ask.MAX_QUESTION} characters.")
        if cid is not None and not (isinstance(cid, str) and cid.isalnum() and len(cid) <= 32):
            raise HTTPException(status_code=422, detail="Bad chat id.")
        q, cid, uid = q.strip(), cid or secrets.token_hex(8), user.id
        with SessionLocal() as db:
            history = [{"role": m.role, "content": m.content} for m in
                       db.query(ask.AskMessage).filter_by(user_id=uid, chat_id=cid).order_by(ask.AskMessage.id).all()]

        def save(role: str, text: str) -> None:
            with SessionLocal() as db:
                db.add(ask.AskMessage(user_id=uid, chat_id=cid, role=role, content=text))
                db.commit()

        def line(**kw) -> bytes:
            return (json.dumps(kw) + "\n").encode()

        def gen():
            client = app.state.ollama
            st = client.status()
            if st["state"] in ("unknown", "nomodel"):
                yield line(error=DOWN if st["state"] == "unknown" else st["reason"], state=st["state"])
                return
            strip, parts = ask.DashStripper(), []
            try:
                for kind, text in client.stream(st["model"], ask.messages_for(context(user), history, q)):
                    if kind == "thinking":
                        yield line(state="thinking")
                        continue
                    out = strip.feed(text)
                    if out:
                        parts.append(out)
                        yield line(token=out)
            except ask.OllamaDown:
                yield line(error=DOWN, state="unknown")
                return
            answer = ("".join(parts) + strip.flush()).strip()
            if not answer:
                yield line(error="The model sent no answer. Try again.")
                return
            save("user", q)
            save("assistant", answer)
            yield line(done=True, chat_id=cid)

        return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
