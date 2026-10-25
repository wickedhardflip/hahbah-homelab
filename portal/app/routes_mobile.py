"""Phone shell and installable app: /m, the manifest, the service worker, the view choice and web push registration.
Mounted by main.create_app with install(). Spec: docs/specs/2026-10-24-mobile-pwa-design.md.
"""
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from . import push
from .auth import current_user, same_origin

STATIC = Path(__file__).parent / "static"
PHONE = ("iphone", "ipod", "android")   # lowercase UA fragments; an iPad or desktop never matches, so they keep the full dashboard
VIEW_COOKIE = "hl_view"


def is_phone(request: Request) -> bool:
    ua = request.headers.get("user-agent", "").lower()
    return any(p in ua for p in PHONE) and "ipad" not in ua and (("android" not in ua) or "mobile" in ua)


def install(app, templates) -> None:
    settings = app.state.settings
    subs_file = settings.data_dir / "push_subs.json"

    @app.middleware("http")
    async def phone_home(request: Request, call_next):
        """A phone opening the dashboard lands on /m, unless the person chose Desktop view (cookie). Nothing else is redirected."""
        if request.method == "GET" and request.url.path == "/" and request.cookies.get(VIEW_COOKIE) != "desktop" and is_phone(request):
            return RedirectResponse("/m", status_code=302, headers={"Cache-Control": "no-store"})
        return await call_next(request)

    @app.get("/m")
    def mobile_page(request: Request, user=Depends(current_user)):
        return templates.TemplateResponse(request, "mobile.html", {
            "user": user, "is_admin": bool(user.is_admin), "push_on": settings.push_configured},
            headers={"Cache-Control": "no-cache"})

    @app.get("/view/{choice}")
    def choose_view(choice: str):
        """The profile menu's Mobile view / Desktop view links. A year-long cookie; no sign-in needed to remember a preference."""
        if choice not in ("mobile", "desktop"):
            raise HTTPException(status_code=404, detail="No such view.")
        r = RedirectResponse("/m" if choice == "mobile" else "/", status_code=303, headers={"Cache-Control": "no-store"})
        r.set_cookie(VIEW_COOKIE, choice, max_age=365 * 86400, samesite="lax", secure=settings.cookie_secure, domain=settings.cookie_domain)
        return r

    # Both are fetched without cookies by the browser, so they must be public. Neither holds anything private.
    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    # ---------- web push ----------
    @app.get("/api/push/config")
    def push_config(user=Depends(current_user)):
        return {"enabled": settings.push_configured, "key": settings.vapid_public if settings.push_configured else ""}

    async def sub_body(request: Request) -> dict:
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if not settings.push_configured:
            raise HTTPException(status_code=404, detail="Notifications are not set up.")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 (not JSON)
            body = None
        if not push.valid(body):
            raise HTTPException(status_code=422, detail="Send a push subscription.")
        return body

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request, user=Depends(current_user)):
        sub = await sub_body(request)
        try:
            ok = push.add(subs_file, sub, user.username)
        except OSError:
            raise HTTPException(status_code=503, detail="Could not save.")
        if not ok:
            raise HTTPException(status_code=409, detail="Too many devices are registered.")
        return JSONResponse({"subscribed": True}, status_code=201)

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request, user=Depends(current_user)):
        sub = await sub_body(request)
        try:
            push.remove(subs_file, [sub["endpoint"]])
        except OSError:
            raise HTTPException(status_code=503, detail="Could not save.")
        return {"subscribed": False}

    @app.post("/api/push/test")
    def push_test(request: Request, user=Depends(current_user)):
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admins only.")
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if not settings.push_configured:
            raise HTTPException(status_code=404, detail="Notifications are not set up.")
        return {"sent": push.send_all(settings, subs_file)}

    # Danger alerts: the recorder calls this once per newly claimed Danger (it already honors mute, acks and maintenance windows).
    app.state.recorder.on_danger = lambda _incidents: push.send_all(settings, subs_file)
