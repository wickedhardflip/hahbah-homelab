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
from zoneinfo import ZoneInfo

from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import models  # noqa: F401  (registers the tables)
from .auth import (LoginLimiter, NotSignedIn, authenticate, create_session, current_user, delete_session,
                   safe_next, same_origin, user_from_request, verify_password, hash_password)
from .auth import _session_id as auth_session_id
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
from .manage import MIN_LENGTH
from .merge import read_collector
from .db import Base, get_db, make_engine, make_sessionmaker, migrate
from .google import GoogleCancelled, GoogleLogin, GoogleStateError, GoogleUnavailable
from .models import Session as SessionRow, User, UserPrefs, utcnow
from . import settingsstore
from .recorder import Recorder, user_addresses
from .registry import load_registry
from .snapshot import SnapshotBuilder, SnapshotStore
from .views import HIDDEN_APP, THUMB_PATH, apps_for, snapshot_for
from .views import discreet as discreet_view, duration, edge_panel, scrub_incidents, whatchanged

HERE = Path(__file__).parent
REEL, REEL_POSTER = "hahbah-reel.mp4", "hahbah-reel.jpg"
MEDIA_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.(mp4|jpg|png|webp)$")
MAINT_KEY = re.compile(r"^(\*|[a-z_]{1,40}:[\w.\-/]{1,80})$")   # a maintenance window's target: an incident key or "*"
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
                           deploy=lambda: last_deploy(settings.deploy_log, history=True), apps=lambda: load_registry(settings.apps_file),
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

    def prefs_of(db, user) -> UserPrefs:
        return db.get(UserPrefs, user.id) or UserPrefs(user_id=user.id, digest=True, instant=True, theme="")

    def user_theme(user) -> str:
        """The <html data-theme> a page starts with: the user's Profile default (a browser's own Day/Midnight choice wins)."""
        with SessionLocal() as db:
            return prefs_of(db, user).theme or "day"

    templates.env.globals["user_theme"] = user_theme

    def mute_state() -> dict:
        """The global alert mute for the header bell; a mute_until in the past counts as unmuted."""
        with SessionLocal() as db:
            until, by = settingsstore.get(db, "mute_until"), settingsstore.get(db, "mute_by")
        muted = bool(until) and until > datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        label = ""
        if muted:
            at = datetime.fromisoformat(until.replace("Z", "+00:00")).astimezone(ZoneInfo(settings.home_tz))
            label = f"Muted by {by or 'someone'} until {at.strftime('%I:%M %p').lstrip('0').lower()}"
        return {"muted": muted, "until": until if muted else "", "by": by if muted else "", "label": label}

    templates.env.globals["mute_state"] = mute_state

    def launcher_apps(user) -> list:
        """Header launcher: the apps this person may see, each with an up/down state from the live snapshot."""
        try:
            apps = apps_for(load_registry(settings.apps_file), user.is_admin, is_discreet())
        except ValueError:
            return []
        state = {n["id"]: n.get("status") for n in store.current().get("nodes", [])}
        return [{"name": a["name"], "url": a["url"], "state": {"good": "up", "crit": "down"}.get(state.get(a["id"]), "unknown")} for a in apps]

    templates.env.globals["launcher_apps"] = launcher_apps

    def set_mute(db, user, hours: int) -> None:
        """hours 0 = unmute. Shared by the header bell and the Settings buttons."""
        until = datetime.now(timezone.utc) + timedelta(hours=hours) if hours else None
        settingsstore.put(db, "mute_until", until.strftime("%Y-%m-%dT%H:%M:%SZ") if until else "")
        settingsstore.put(db, "mute_by", user.username if until else "")
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

    def clock(t: datetime) -> str:
        """'3:40 pm' in home time, with the date when it isn't today."""
        tz = ZoneInfo(settings.home_tz)
        local = t.astimezone(tz)
        hm = local.strftime("%I:%M %p").lstrip("0").lower()
        return hm if local.date() == datetime.now(tz).date() else f"{local.strftime('%b')} {local.day}, {hm}"

    def hidden_key(key: str) -> bool:
        """Discreet mode: keys about the hidden app can't be acked or put in maintenance, nor their existence confirmed."""
        if not is_discreet():
            return False
        keys = lambda s: {f"{a.get('kind')}:{a.get('target')}" for a in s.get("alerts", [])}
        return key.split(":", 1)[-1] == HIDDEN_APP or (key in keys(store.current()) and key not in keys(discreet_view(store.current())))

    def with_acks(snap: dict) -> dict:
        """Each alert says whether it has an OPEN incident (`incident`, so the card shows Acknowledge) and who acked it,
        and whether a maintenance window covers it (`maint_label`). Incidents open after 2 checks in a row, so an alert
        in its first check has no button yet. Copies, never mutates the shared snapshot; runs after snapshot_for, so
        Discreet mode has already dropped what it hides."""
        acks, active = recorder.open_acks(), recorder.active_windows()
        none = {"acked_by": None, "acked_at": None}
        def one(a):
            key = f"{a.get('kind')}:{a.get('target')}"
            got, until = acks.get(key), recorder.held_until(active, key)
            at = got and got["acked_at"]
            return {**a, "incident": got is not None, **(got or none), "acked_at": at.isoformat() if at else None,
                    "maint_until": until.isoformat() if until else None,
                    "maint_label": f"In maintenance until {clock(until)}" if until else ""}
        return {**snap, "alerts": [one(a) for a in snap.get("alerts", [])],
                "maintenance": [{"target": t, "end": e.isoformat()} for t, e in active.items() if not hidden_key(t)]}

    def visible_view(user) -> dict:
        return with_acks(snapshot_for(store.current(), user.is_admin, is_discreet()))

    @app.get("/")
    def dashboard(request: Request, user=Depends(current_user)):
        mode = is_discreet()
        snap = visible_view(user)
        changed = whatchanged(snap, recorder.recent_incidents(hours=24 * 14, limit=60), datetime.now(timezone.utc),
                              settings.home_tz, show_subjects=bool(user.is_admin) and not mode, discreet_mode=mode)
        return templates.TemplateResponse(request, "dashboard.html",
                                          {"snapshot": snap, "user": user, "changed": changed,
                                           "is_admin": bool(user.is_admin)})

    @app.get("/incidents")
    def incidents_page(request: Request, severity: str = Query(""), state: str = Query(""), page: str = Query("1"),
                       user=Depends(current_user)):
        mode, per = is_discreet(), 25
        severity, state = severity if severity in ("crit", "warn") else "", state if state in ("open", "closed") else ""
        page = max(1, int(page)) if page.isascii() and page.isdigit() else 1
        rows, total = recorder.history(severity, state, page, per, lambda r: scrub_incidents(r, bool(user.is_admin) and not mode, mode))
        pages = max(1, -(-total // per))
        if page > pages:
            page = pages
            rows, total = recorder.history(severity, state, page, per, lambda r: scrub_incidents(r, bool(user.is_admin) and not mode, mode))
        tz = ZoneInfo(settings.home_tz)
        at = lambda t: t.astimezone(tz).strftime("%b %d, %I:%M %p").replace(" 0", " ") if t else ""
        rows = [{**r, "opened": at(r["opened_at"]), "dur": duration(r["opened_at"], r["closed_at"])} for r in rows]
        return templates.TemplateResponse(request, "incidents.html", {
            "user": user, "rows": rows, "total": total, "page": page, "pages": pages, "severity": severity, "state": state})

    # ---------- Maintenance windows (any signed-in user) ----------
    def maintenance_page(request: Request, user, target: str = "", error: str = "", saved: str = "", status: int = 200):
        snap, opts = visible_view(user), {"*": "Everything"}
        for a in snap.get("alerts", []):
            if a.get("severity") in ("warn", "crit"):
                opts.setdefault(f"{a['kind']}:{a['target']}", f"{a['target']} · {a['message']}"[:90])
        try:
            for a in apps_for(load_registry(settings.apps_file), user.is_admin, is_discreet()):
                opts.setdefault(f"app_down:{a['id']}", f"{a['name']} (app down)")
        except ValueError:
            pass
        if MAINT_KEY.fullmatch(target) and not hidden_key(target):
            opts.setdefault(target, target)
        rows = [{**w, "from": clock(w["start"]), "to": clock(w["end"]), "label": opts.get(w["target"], w["target"])}
                for w in recorder.windows() if not hidden_key(w["target"])]
        return templates.TemplateResponse(request, "maintenance.html", {
            "user": user, "opts": opts, "target": target, "error": error, "saved": saved,
            "active": [w for w in rows if w["state"] == "active"], "upcoming": [w for w in rows if w["state"] == "upcoming"],
            "past": [w for w in rows if w["state"] == "past"][:20]}, status_code=status)

    @app.get("/maintenance")
    def maintenance_get(request: Request, target: str = Query(""), saved: str = Query(""), user=Depends(current_user)):
        return maintenance_page(request, user, target, saved={"added": "Window saved.", "cancelled": "Window cancelled."}.get(saved, ""))

    @app.post("/api/maintenance")
    def maintenance_add(request: Request, target: str = Form(""), start: str = Form(""), minutes: str = Form(""),
                        end: str = Form(""), note: str = Form(""), user=Depends(current_user)):
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if hidden_key(target):
            raise HTTPException(status_code=404, detail="Unknown target.")
        tz, note = ZoneInfo(settings.home_tz), note.strip()
        local = lambda v: datetime.strptime(v, "%Y-%m-%dT%H:%M").replace(tzinfo=tz)   # <input type=datetime-local>, home time
        try:
            t0 = local(start) if start else recorder.now()
            t1 = local(end) if minutes == "custom" else t0 + timedelta(minutes={"30": 30, "120": 120, "480": 480}[minutes])
        except (ValueError, KeyError):
            t0 = t1 = None
        error = ("Pick what the window covers." if not MAINT_KEY.fullmatch(target)
                 else "Enter a valid start and end." if t0 is None
                 else "The end must be after the start." if t1 <= t0
                 else "A window can last at most 14 days." if t1 - t0 > timedelta(days=14)
                 else "Keep the note to 140 characters." if len(note) > 140 else "")
        if error:
            return maintenance_page(request, user, target, error=error, status=400)
        recorder.add_window(target, t0, t1, note, user.username)
        return RedirectResponse("/maintenance?saved=added", status_code=303)

    @app.post("/api/maintenance/{wid}/cancel")
    def maintenance_cancel(request: Request, wid: int, user=Depends(current_user)):
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        w = next((w for w in recorder.windows() if w["id"] == wid), None)
        if w is None or hidden_key(w["target"]) or not recorder.cancel_window(wid):
            raise HTTPException(status_code=404, detail="No such window.")
        return RedirectResponse("/maintenance?saved=cancelled", status_code=303)

    @app.get("/about")
    def about(request: Request, user=Depends(current_user)):
        snap = snapshot_for(store.current(), user.is_admin, is_discreet())
        return templates.TemplateResponse(request, "about.html", {
            "user": user, "sources": snap.get("sources", []), "lines": (snap.get("layout") or {}).get("lines", []),
            "video": (settings.media_dir / REEL).is_file(), "poster": (settings.media_dir / REEL_POSTER).is_file(),
            "reel": REEL, "reel_poster": REEL_POSTER, "is_admin": bool(user.is_admin),
            "v": int((settings.media_dir / REEL).stat().st_mtime) if (settings.media_dir / REEL).is_file() else 0})   # cache-buster

    # ---------- Profile (any signed-in user) ----------
    def profile_page(request: Request, user, note: str = "", bad: bool = False, status: int = 200):
        with SessionLocal() as db:
            prefs = prefs_of(db, user)
        return templates.TemplateResponse(request, "profile.html", {
            "user": user, "note": note, "bad": bad, "min_length": MIN_LENGTH, "prefs": prefs,
            "emails": sorted(user_addresses(user.username, user.google_email)),
            "since": user.created_at.strftime("%B %Y") if user.created_at else "unknown"}, status_code=status)

    @app.get("/profile")
    def profile_get(request: Request, saved: str = Query(""), user=Depends(current_user)):
        return profile_page(request, user, {"password": "Password changed. Your other sign-ins were ended.",
                                            "unlinked": "Google account unlinked.", "prefs": "Preferences saved."}.get(saved, ""))

    @app.post("/profile/password")
    def profile_password(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(""),
                         user=Depends(current_user), db=Depends(get_db)):
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if not app.state.limiter.try_acquire(f"pw:{user.id}"):
            return profile_page(request, user, "Too many attempts. Try again in 15 minutes.", True, 429)
        error = ("Current password is wrong." if not verify_password(current, user.password_hash)
                 else "The new passwords don't match." if new != confirm
                 else f"Use at least {MIN_LENGTH} characters." if len(new) < MIN_LENGTH else "")
        if error:
            return profile_page(request, user, error, True, 400)
        app.state.limiter.reset(f"pw:{user.id}")
        user.password_hash = hash_password(new)
        keep = auth_session_id(request.cookies.get(settings.cookie_name, ""))
        db.query(SessionRow).filter(SessionRow.user_id == user.id, SessionRow.id != keep).delete()
        db.commit()
        return RedirectResponse("/profile?saved=password", status_code=303)

    @app.post("/profile/prefs")
    def profile_prefs(request: Request, digest: str = Form(""), instant: str = Form(""), theme: str = Form(""),
                      user=Depends(current_user), db=Depends(get_db)):
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if theme not in ("", "day", "midnight"):
            raise HTTPException(status_code=400, detail="Unknown theme.")
        db.merge(UserPrefs(user_id=user.id, digest=bool(digest), instant=bool(instant), theme=theme))   # unticked box = off
        db.commit()
        return RedirectResponse("/profile?saved=prefs", status_code=303)

    @app.post("/profile/google/unlink")
    def profile_google_unlink(request: Request, current: str = Form(""), user=Depends(current_user), db=Depends(get_db)):
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if not verify_password(current, user.password_hash):   # a Google-only user may not know the admin-set password
            return profile_page(request, user, "Enter your current password to unlink Google, so you can still sign in.", True, 400)
        user.google_email = None
        db.commit()
        return RedirectResponse("/profile?saved=unlinked", status_code=303)

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
            "saved": saved, "tz": settings.home_tz, "edge": edge_panel(snap.get("edge"), datetime.now(timezone.utc)),
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
                set_mute(db, user, hours)
                note = "muted"
            elif action == "unmute":
                set_mute(db, user, 0)
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
        return {**visible_view(user), "mute": mute_state()}

    def ack_route(request: Request, key: str, user, undo: bool) -> dict:
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if hidden_key(key):
            raise HTTPException(status_code=404, detail="No open incident with that key.")
        if not (recorder.unack(key) if undo else recorder.ack(key, user.username)):
            raise HTTPException(status_code=404, detail="No open incident with that key.")
        return {"key": key, **recorder.open_acks().get(key, {"acked_by": None, "acked_at": None})}

    @app.post("/api/incidents/{key}/ack")
    def api_ack(request: Request, key: str, user=Depends(current_user)):
        return ack_route(request, key, user, undo=False)

    @app.post("/api/incidents/{key}/unack")
    def api_unack(request: Request, key: str, user=Depends(current_user)):
        return ack_route(request, key, user, undo=True)

    @app.post("/api/mute")
    async def api_mute(request: Request, user=Depends(current_user)):
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        try:
            hours = (await request.json())["hours"]
        except Exception:  # noqa: BLE001 (not JSON / no hours: same answer)
            hours = None
        if isinstance(hours, bool) or hours not in (0, 1, 4, 24):
            raise HTTPException(status_code=400, detail="hours must be 0, 1, 4 or 24.")
        with SessionLocal() as db:
            set_mute(db, user, hours)
        return mute_state()

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
