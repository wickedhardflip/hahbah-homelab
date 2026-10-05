"""The portal: sign-in, the dashboard, and the JSON API behind it."""
import asyncio
import html
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import models  # noqa: F401  (registers the tables)
from .auth import (LoginLimiter, NotSignedIn, authenticate, create_session, current_user, delete_session,
                   safe_next, same_origin, user_from_request)
from .config import Settings, load_settings
from .collectors.deploy import last_deploy
from .collectors.host import HostSampler
from .collectors.sportsteams import INVALID, SLOTS, menu, parse_form, write_picks
from .collectors.weather import geocode, write_location
from .collectors import tautulli
from .collectors.tautulli import local_today, plex_stats
from .collectors.upnp import eero_status
from .collectors.cert import fetch_der, parse_cert
from .collectors.health import check_all, health_targets
from .mailer import MAX_WEB, parse_recipients
from .merge import read_collector
from .db import Base, get_db, make_engine, make_sessionmaker, migrate
from .google import GoogleCancelled, GoogleLogin, GoogleStateError, GoogleUnavailable
from .models import Session as SessionRow, User, utcnow
from . import settingsstore
from .recorder import Recorder
from .registry import load_registry
from .snapshot import SnapshotBuilder, SnapshotStore
from .views import THUMB_PATH, apps_for, snapshot_for

HERE = Path(__file__).parent
REEL, REEL_POSTER = "hahbah-reel.mp4", "hahbah-reel.jpg"
MEDIA_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.(mp4|jpg|png|webp)$")
MEDIA_TYPES = {"mp4": "video/mp4", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
log = logging.getLogger("portal")


class CachedStatic(StaticFiles):
    """Vendored libraries and images rarely change: a day. The portal's own scripts revalidate (ETag, so a 304) on every load."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        own = str(full_path).replace("\\", "/").rsplit("/static/", 1)[-1]
        resp.headers["Cache-Control"] = "no-cache" if own.endswith((".js", ".css")) and not own.startswith("vendor/") \
            else "public, max-age=86400"
        return resp


def default_builder(settings: Settings, trends=lambda: {}) -> SnapshotBuilder:
    topology = json.loads((HERE / "topology.json").read_text(encoding="utf-8"))
    host = HostSampler(settings.host_proc, settings.host_sys, settings.nic, settings.disks)
    sni = f"home.{settings.base_domain}"
    return SnapshotBuilder(topology, host=host.sample, eero=lambda: eero_status(settings.eero_igd_url),
                           deploy=lambda: last_deploy(settings.deploy_log), apps=lambda: load_registry(settings.apps_file),
                           plex=lambda: plex_stats(settings.tautulli_url, settings.tautulli_api_key, local_today(settings.home_tz)),
                           cert=lambda: parse_cert(fetch_der(settings.caddy_host, 443, sni)),
                           health=lambda: check_all(health_targets(load_registry(settings.apps_file), settings.host_gateway,
                                                                   settings.ollama_url)),
                           collector=lambda: read_collector(settings.collector_dir, datetime.now(timezone.utc)),
                           tz=settings.home_tz, trends=trends)


async def collect_forever(store: SnapshotStore, interval: int, after=None) -> None:
    while True:
        try:
            snap = await asyncio.to_thread(store.refresh)
            if after is not None:
                try:
                    await asyncio.to_thread(after, snap)   # incidents, emails, daily numbers, digest
                except Exception:
                    log.exception("recording incidents/metrics failed")
        except Exception:
            log.exception("snapshot refresh failed; keeping the previous one")
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None, builder=None, collect: bool = True) -> FastAPI:
    settings = settings or load_settings()
    engine = make_engine(settings.data_dir)
    Base.metadata.create_all(engine)
    migrate(engine)

    SessionLocal = make_sessionmaker(engine)
    outbox = settings.outbox_dir or None   # the writer creates the directory on each write
    recorder = Recorder(SessionLocal, outbox, settings.alert_to, tz=settings.home_tz, mail_dir=settings.mail_dir)
    builder = builder or default_builder(settings, trends=recorder.trends)
    store = SnapshotStore(builder.build)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(collect_forever(store, settings.collect_interval, recorder.observe)) if collect else None
        yield
        if task:
            task.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.SessionLocal = SessionLocal
    app.state.store = store
    app.state.recorder = recorder
    app.mount("/static", CachedStatic(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    app.state.limiter = LoginLimiter()
    app.state.tautulli_get = tautulli.default_get
    app.state.tautulli_get_raw = tautulli.default_get_raw
    app.state.google = GoogleLogin(settings.google_client_id, settings.google_client_secret) if settings.google_configured else None
    # Holds Authlib's state + nonce and `next` for 10 minutes during a Google sign-in.
    app.add_middleware(SessionMiddleware, secret_key=settings.oauth_state_secret or secrets.token_urlsafe(32),
                       session_cookie="hl_oauth", max_age=600, same_site="lax", https_only=settings.cookie_secure)

    def login_page_response(request: Request, error: str | None, status: int, target: str | None, username: str = ""):
        return templates.TemplateResponse(request, "login.html",
            {"error": error, "username": username, "next": target or "", "google": app.state.google is not None},
            status_code=status)

    def signed_in(db, user, target: str | None):
        token = create_session(db, user, settings.session_days)
        response = RedirectResponse(target or "/", status_code=303)
        response.set_cookie(settings.cookie_name, token, max_age=settings.session_days * 86400,
                            httponly=True, secure=settings.cookie_secure, samesite="lax",
                            domain=settings.cookie_domain)
        return response

    def is_discreet() -> bool:
        with SessionLocal() as db:
            return settingsstore.get(db, "discreet") == "1"

    def admin_user(user=Depends(current_user)):
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admins only.")
        return user

    @app.exception_handler(NotSignedIn)
    async def _not_signed_in(request: Request, _exc: NotSignedIn):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Sign in required."}, status_code=401)
        if request.headers.get("hx-request"):   # HTMX would swap a redirected login page into the fragment; send the browser there instead
            return Response(status_code=401, headers={"HX-Redirect": "/login", "Cache-Control": "no-store"})
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login")
    def login_page(request: Request, next: str | None = Query(None), db=Depends(get_db)):
        target = safe_next(next, settings.base_domain)
        if target and user_from_request(request, db):
            return RedirectResponse(target, status_code=303)
        return login_page_response(request, None, 200, target)

    @app.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form(""),
              db=Depends(get_db)):
        limiter, key = app.state.limiter, request.client.host if request.client else "unknown"
        target = safe_next(next, settings.base_domain)
        if not same_origin(request, settings.home_url):
            return login_page_response(request, "Sign in from this page.", 403, target, username)
        if not limiter.try_acquire(key):
            return login_page_response(request, "Too many failed sign-ins. Try again in 15 minutes.", 429, target, username)
        user = authenticate(db, username, password)
        if not user:
            return login_page_response(request, "Wrong username or password.", 401, target, username)
        limiter.reset(key)
        return signed_in(db, user, target)

    @app.get("/auth/google")
    async def google_start(request: Request, next: str | None = Query(None)):
        google = app.state.google
        if google is None:
            raise HTTPException(status_code=404)
        target = safe_next(next, settings.base_domain)
        # One entry per sign-in, keyed by its state, so two tabs signing in at once don't overwrite each other.
        state = secrets.token_urlsafe(24)
        pending = [k for k in request.session if k.startswith("next:")]
        for k in pending[:-4]:   # keep the cookie small: the four most recent sign-ins are plenty
            request.session.pop(k, None)
        request.session[f"next:{state}"] = target or ""
        try:
            return await google.redirect(request, f"{settings.home_url}/auth/google/callback", state=state)
        except GoogleUnavailable as e:
            log.warning("Google sign-in couldn't start: %s", e)
            return login_page_response(request, "Couldn't reach Google. Use your password.", 502, target)

    @app.get("/auth/google/callback")
    async def google_callback(request: Request, db=Depends(get_db)):
        google = app.state.google
        if google is None:
            raise HTTPException(status_code=404)
        target = safe_next(request.session.pop(f"next:{request.query_params.get('state', '')}", ""), settings.base_domain)
        try:
            email, verified = await google.identity(request)
        except GoogleCancelled:
            return login_page_response(request, "Google sign-in was cancelled.", 400, target)
        except GoogleStateError:
            return login_page_response(request, "Sign-in took too long. Try again.", 400, target)
        except GoogleUnavailable as e:
            log.warning("Google sign-in failed: %s", e)
            return login_page_response(request, "Couldn't reach Google. Use your password.", 502, target)
        user = db.query(User).filter(User.google_email == email).first() if email and verified else None
        if user is None:
            log.info("Refused Google sign-in for %s", email or "(no email)")
            return login_page_response(request, "That Google account isn't allowed here.", 403, target)
        return signed_in(db, user, target)

    @app.get("/auth/verify")
    def auth_verify(request: Request, role: str | None = Query(None), db=Depends(get_db)):
        """Caddy forward_auth: 200 lets the request through to the app; anything else goes back to the visitor.
        role=admin (apps marked `admin: true`) also needs an admin account."""
        user = user_from_request(request, db)
        if user and role == "admin" and not user.is_admin:
            home = html.escape(settings.home_url, quote=True)
            return Response(f'<!doctype html><title>Admins only</title><p>Admins only. <a href="{home}">Back to the dashboard</a></p>',
                            status_code=403, media_type="text/html", headers={"Cache-Control": "no-store"})
        if user:
            return Response(status_code=200, headers={"Remote-User": user.username, "Cache-Control": "no-store"})
        original = f"https://{request.headers.get('x-forwarded-host', '')}{request.headers.get('x-forwarded-uri', '/')}"
        target = f"{settings.home_url}/login"
        if safe_next(original, settings.base_domain):
            target += "?next=" + quote(original, safe="")
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    @app.get("/logout")
    def logout_page(request: Request, db=Depends(get_db)):
        """Where the apps' own Sign out links land under single sign-on; signing out is still a POST."""
        user = user_from_request(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"signout": True, "username": user.username})

    @app.post("/logout")
    def logout(request: Request, db=Depends(get_db)):
        if not same_origin(request, settings.home_url):
            return Response("Sign out from the dashboard.", status_code=403, media_type="text/plain")
        token = request.cookies.get(settings.cookie_name)
        if token:
            delete_session(db, token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(settings.cookie_name, domain=settings.cookie_domain)
        return response

    @app.get("/")
    def dashboard(request: Request, user=Depends(current_user)):
        return templates.TemplateResponse(request, "dashboard.html",
                                          {"snapshot": snapshot_for(store.current(), user.is_admin, is_discreet()), "user": user,
                                           "is_admin": bool(user.is_admin)})

    @app.get("/about")
    def about(request: Request, user=Depends(current_user)):
        snap = snapshot_for(store.current(), user.is_admin, is_discreet())
        return templates.TemplateResponse(request, "about.html", {
            "user": user, "sources": snap.get("sources", []), "lines": (snap.get("layout") or {}).get("lines", []),
            "video": (settings.media_dir / REEL).is_file(), "poster": (settings.media_dir / REEL_POSTER).is_file(),
            "reel": REEL, "reel_poster": REEL_POSTER, "is_admin": bool(user.is_admin),
            "v": int((settings.media_dir / REEL).stat().st_mtime) if (settings.media_dir / REEL).is_file() else 0})   # cache-buster

    # ---------- Settings (admins only) ----------
    def settings_page(request: Request, user, saved: str = "", status: int = 200):
        with SessionLocal() as db:
            values = settingsstore.get_all(db)
            live = dict(db.query(SessionRow.user_id, func.count()).filter(SessionRow.expires_at > utcnow())
                        .group_by(SessionRow.user_id).all())   # one query, not one per user
            users = [{"id": u.id, "name": u.username, "admin": u.is_admin, "google": u.google_email or "",
                      "sessions": live.get(u.id, 0)} for u in db.query(User).order_by(User.username).all()]
        snap = store.current()
        outbox = {"queued": 0, "failed": 0, "on": recorder.outbox is not None and bool(recorder.to)}
        if recorder.outbox is not None:
            outbox["queued"] = len(list(Path(recorder.outbox).glob("*.eml")))
            outbox["failed"] = len(list((Path(recorder.outbox) / "failed").glob("*.eml")))
        alerts_off, report_off = (set(parse_recipients(values.get(k, ""))) for k in ("alerts_off", "report_off"))
        recipients = [{"addr": a, "alerts": a not in alerts_off, "report": a not in report_off, "owner": a in recorder.owners}
                      for a in recorder.recipients]
        mute = values.get("mute_until") or ""
        muted = bool(mute) and mute > datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return templates.TemplateResponse(request, "settings.html", {
            "user": user, "s": values, "times": settingsstore.DIGEST_TIMES, "users": users, "outbox": outbox,
            "recipients": recipients, "web_on": bool(recorder.mail_dir),
            "can_add": bool(recorder.mail_dir) and len(recorder.web_recipients()) < MAX_WEB,
            "sources": snap.get("sources", []), "deploy": (snap.get("edge") or {}).get("deploy") or {},
            "incidents": recorder.recent_incidents(hours=24 * 14, limit=40), "muted": muted, "mute_until": mute,
            "saved": saved, "tz": settings.home_tz,
            "sports_menus": {slot: menu(slot) for slot in SLOTS}}, status_code=status)

    @app.get("/settings")
    def settings_get(request: Request, saved: str = Query(""), user=Depends(admin_user)):
        return settings_page(request, user, saved)

    @app.post("/settings")
    def settings_post(request: Request, action: str = Form("save"), discreet: str = Form(""), digest_enabled: str = Form(""),
                      alerts_enabled: str = Form(""), digest_time: str = Form(""), hours: int = Form(0), user_id: int = Form(0),
                      recipients_form: str = Form(""), idle_minutes: str = Form(""), address: str = Form(""), place: str = Form(""), alert_to: list[str] = Form([]), report_to: list[str] = Form([]),
                      ballpark: str = Form(""), arena: str = Form(""), busstop: str = Form(""),
                      user=Depends(admin_user)):
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        note = "saved"
        with SessionLocal() as db:
            if action == "save":
                settingsstore.put(db, "discreet", "1" if discreet else "0")
                settingsstore.put(db, "digest_enabled", "1" if digest_enabled else "0")
                settingsstore.put(db, "alerts_enabled", "1" if alerts_enabled else "0")
                if idle_minutes.strip() != "":   # blank = field not on the form; an out-of-range number is refused, not clamped
                    idle = settingsstore.parse_idle(idle_minutes)
                    if idle is None:
                        note = "idle-invalid"
                    else:
                        settingsstore.put(db, "idle_minutes", str(idle))
                if digest_time in settingsstore.DIGEST_TIMES:
                    settingsstore.put(db, "digest_time", digest_time)
                if recipients_form:   # only addresses on the server's allowlist can be switched; the rest is ignored
                    for key, on in (("alerts_off", set(alert_to)), ("report_off", set(report_to))):
                        settingsstore.put(db, key, ",".join(a for a in recorder.recipients if a not in on))
            elif action == "add_address":
                note = recorder.add_web(address, user.username)
            elif action == "remove_address":
                note = recorder.remove_web(address, user.username)
            elif action == "make_primary":
                note = recorder.make_primary(address, user.username)
            elif action == "weather":   # city/ZIP -> Open-Meteo geocoding; an unknown place keeps the old location
                note = "weather-unknown"
                try:
                    hit = geocode(place)
                except Exception:  # noqa: BLE001 (network, bad JSON...: say so, keep the old place)
                    hit, note = None, "weather-unreachable"
                if hit:
                    for k, v in (("weather_name", hit["name"]), ("weather_lat", str(hit["lat"])), ("weather_lon", str(hit["lon"]))):
                        settingsstore.put(db, k, v)
                    if settings.weather_dir:   # the collector reads this file (validated again on its side)
                        try:
                            write_location(settings.weather_dir / "location.json", hit["name"], hit["lat"], hit["lon"])
                        except OSError:
                            log.warning("could not write the weather location file")
                    note = "weather:" + hit["name"]
            elif action == "sports":   # three dropdowns -> validated picks; a value that isn't on our lists changes nothing
                picks = {slot: parse_form(slot, raw) for slot, raw in zip(SLOTS, (ballpark, arena, busstop))}
                if any(v is INVALID for v in picks.values()):
                    note = "sports-invalid"
                else:
                    for slot, v in picks.items():
                        settingsstore.put(db, f"sports_{slot}", "none" if v is None else str(v))
                    if settings.sports_file:   # the collector reads this file (validated again on its side)
                        try:
                            write_picks(settings.sports_file, picks)
                        except OSError:
                            log.warning("could not write the sports picks file")
                    note = "sports"
            elif action == "mute" and hours in (1, 4, 24):
                until = datetime.now(timezone.utc) + timedelta(hours=hours)
                settingsstore.put(db, "mute_until", until.strftime("%Y-%m-%dT%H:%M:%SZ"))
                note = "muted"
            elif action == "unmute":
                settingsstore.put(db, "mute_until", "")
                note = "unmuted"
            elif action == "test_email":
                ok = recorder.send_test()
                note = "test-sent" if ok else "test-failed"
            elif action == "signout_user" and user_id:
                db.query(SessionRow).filter(SessionRow.user_id == user_id).delete()
                db.commit()
                note = "signed-out"
            else:
                note = ""
        return RedirectResponse(f"/settings?saved={quote(note)}", status_code=303)

    @app.get("/media/{name}")
    def media(name: str, user=Depends(current_user)):
        """Signed-in only, a fixed set of file types, no paths: only files sitting directly in media_dir."""
        path = settings.media_dir / name
        if not MEDIA_NAME.fullmatch(name) or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type=MEDIA_TYPES[name.rsplit(".", 1)[1]],
                            headers={"Cache-Control": "private, max-age=86400"})   # FileResponse handles Range (iPhone video)

    @app.post("/api/speedtest")
    def api_speedtest(request: Request, user=Depends(admin_user)):
        """Ask the collector for a speed test now. One at a time: a request newer than the last result is still pending."""
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if not settings.speed_request:
            raise HTTPException(status_code=404, detail="Speed tests on demand are not set up.")
        try:
            last = datetime.fromisoformat(json.loads((settings.collector_dir / "speed.json").read_text(encoding="utf-8"))
                                          ["checked_at"].replace("Z", "+00:00")).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            last = 0.0
        try:
            if settings.speed_request.stat().st_mtime > last:
                return {"status": "running"}
        except OSError:
            pass
        try:
            settings.speed_request.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        except OSError:
            raise HTTPException(status_code=503, detail="Could not ask for a speed test.")
        return {"status": "started"}

    @app.get("/api/snapshot")
    def api_snapshot(user=Depends(current_user)):
        return snapshot_for(store.current(), user.is_admin, is_discreet())

    @app.get("/api/apps")
    def api_apps(user=Depends(current_user)):
        try:
            return {"apps": apps_for(load_registry(settings.apps_file), user.is_admin, is_discreet())}
        except ValueError as e:
            return {"apps": [], "error": str(e)}

    @app.get("/api/plex/user/{user_id}")
    def api_plex_user(user_id: int, user=Depends(admin_user)):
        try:
            return {"plays": tautulli.user_history(settings.tautulli_url, settings.tautulli_api_key, user_id,
                                                   get=app.state.tautulli_get)}
        except tautulli.TautulliError as e:
            return JSONResponse({"detail": str(e)}, status_code=502)

    @app.get("/api/plex/thumb")
    def api_plex_thumb(path: str = Query(""), w: int = Query(120), user=Depends(admin_user)):
        if not THUMB_PATH.fullmatch(path):
            return JSONResponse({"detail": "Not a Plex poster path."}, status_code=400)
        try:
            body, ctype = tautulli.image(settings.tautulli_url, settings.tautulli_api_key, path, max(60, min(400, w)),
                                         get_raw=app.state.tautulli_get_raw)
        except tautulli.TautulliError as e:
            return JSONResponse({"detail": str(e)}, status_code=502)
        return Response(body, media_type=ctype if ctype.startswith("image/") else "application/octet-stream",
                        headers={"Cache-Control": "private, max-age=3600"})

    return app
