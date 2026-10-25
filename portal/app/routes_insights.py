"""Insights page and API: disk-full forecast, ISP report card, weekly digest preview. Mounted by main.create_app with install().

Only reads what the portal already keeps. The weekly digest email is OFF until an admin switches it on (setting
`insights_digest`); the preview below never sends anything."""
import email
import email.policy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import insights, settingsstore
from .auth import current_user, same_origin


def install(app, templates) -> None:
    settings, SessionLocal, recorder = app.state.settings, app.state.SessionLocal, app.state.recorder

    def admin_user(user=Depends(current_user)):
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admins only.")
        return user

    def origin(request: Request) -> None:
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")

    def build() -> dict:
        with SessionLocal() as db:
            return insights.report(db, datetime.now(timezone.utc), settings.home_tz)

    def weekly_tick(snap=None) -> None:
        """Runs after each snapshot: queue the weekly digest once per week, only when switched on."""
        now = recorder.now()
        local = now.astimezone(ZoneInfo(settings.home_tz))
        with SessionLocal() as db:
            st = settingsstore.get_all(db)
            if not insights.weekly_due(st, local):
                return
            rep = insights.report(db, now, settings.home_tz)
            y, w, _ = local.isocalendar()
            settingsstore.put(db, "insights_digest_week", f"{y}-{w:02d}")
        if recorder.to:
            recorder._send(insights.weekly_message(rep, recorder.to, now, settings.home_tz), "digest")

    observe = recorder.observe   # the collector loop reads recorder.observe when it starts, so this wraps it
    def observe_and_digest(snap: dict) -> None:
        observe(snap)
        try:
            weekly_tick(snap)
        except Exception:  # noqa: BLE001 (the digest must never stop alerts)
            pass
    recorder.observe = observe_and_digest

    @app.get("/insights")
    def insights_page(request: Request, user=Depends(current_user)):
        with SessionLocal() as db:
            on = settingsstore.get(db, "insights_digest") == "1"
        return templates.TemplateResponse(request, "insights.html", {"user": user, "is_admin": bool(user.is_admin), "digest_on": on})

    @app.get("/api/insights")
    def api_insights(user=Depends(current_user)):
        return build()

    @app.get("/insights/digest-preview", response_class=HTMLResponse)
    def digest_preview(user=Depends(admin_user)):
        """The weekly email as it would be written now. Nothing is queued or sent."""
        raw = insights.weekly_message(build(), "preview@example.com", datetime.now(timezone.utc), settings.home_tz)
        part = email.message_from_bytes(raw, policy=email.policy.SMTP).get_body(("html",))
        return HTMLResponse(part.get_content(), headers={"Cache-Control": "no-store"})

    @app.post("/api/insights/digest")
    def set_digest(body: dict, user=Depends(admin_user), _=Depends(origin)):
        on = body.get("enabled") is True
        with SessionLocal() as db:
            settingsstore.put(db, "insights_digest", "1" if on else "0")
        return {"enabled": on}
