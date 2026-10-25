"""Presence + automations pages and API (spec: docs/specs/2026-10-24-automations-design.md). Mounted by main.create_app with install().

Reads what the collector writes (/collector/automations.json, wyze.json) and writes people.json + rules.json into the shared plugs folder.
Any signed-in user may read; every write is admin-only with the same Origin check as the plug API.
"""
import json
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Query, Request

from . import automations as auto
from . import presence, settingsstore
from .auth import current_user, same_origin
from .plugs import write_json

STALE_S = 900   # automations.json is rewritten at least every 4 min; older means the collector is not running it


def install(app, templates) -> None:
    settings, SessionLocal = app.state.settings, app.state.SessionLocal
    write_lock = threading.Lock()

    def admin_user(user=Depends(current_user)):
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admins only.")
        return user

    def state() -> dict:
        """The collector's presence + rule state, marked stale when old or missing. Never raises."""
        try:
            doc = json.loads((settings.collector_dir / "automations.json").read_text(encoding="utf-8"))
            data, at = doc["data"], presence.parse(doc["checked_at"])
            if not isinstance(data, dict) or at is None:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            return {**presence.EMPTY, "rules": {}, "pending": []}
        if (datetime.now(timezone.utc) - at).total_seconds() > STALE_S:
            data = {**data, "stale": True}
        return data

    def people_cfg() -> dict:
        return presence.read_people(settings.plugs_dir / "people.json") if settings.plugs_dir else {"people": [], "away_after_min": presence.AWAY_DEFAULT}

    def people_now(st: dict) -> list:
        """People as configured (a fresh save shows at once), with home/since from the collector when it has them."""
        live = {p["id"]: p for p in st.get("people", [])}
        return [{**p, "home": live.get(p["id"], {}).get("home"), "since": live.get(p["id"], {}).get("since")} for p in people_cfg()["people"]]

    def location():
        with SessionLocal() as db:
            lat, lon = settingsstore.get(db, "weather_lat"), settingsstore.get(db, "weather_lon")
        try:
            return {"lat": float(lat), "lon": float(lon)}
        except ValueError:
            return None

    def ctx(st: dict) -> auto.Ctx:
        return auto.Ctx(datetime.now(timezone.utc), ZoneInfo(settings.home_tz), people_now(st), bool(st.get("stale", True)),
                        auto.plug_rows(settings.collector_dir, settings.plugs_dir), location())

    async def body_of(request: Request) -> dict:
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")
        if not settings.plugs_dir:
            raise HTTPException(status_code=404, detail="Automations are not set up.")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 (not JSON)
            body = None
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="Send a JSON object.")
        return body

    def save(name: str, doc: dict) -> None:
        try:
            with write_lock:
                write_json(settings.plugs_dir / name, doc)
        except OSError:
            raise HTTPException(status_code=503, detail="Could not save.")

    # ---------- pages ----------
    @app.get("/presence")
    def presence_page(request: Request, user=Depends(current_user)):
        return templates.TemplateResponse(request, "presence.html", {"user": user, "is_admin": bool(user.is_admin)})

    @app.get("/automations")
    def automations_page(request: Request, user=Depends(current_user)):
        return templates.TemplateResponse(request, "automations.html", {"user": user, "is_admin": bool(user.is_admin)})

    # ---------- presence ----------
    @app.get("/api/presence")
    def api_presence(user=Depends(current_user)):
        st = state()
        out = presence.public({**st, "people": people_now(st)}, bool(user.is_admin))
        return {"configured": bool(settings.plugs_dir), **out}

    @app.put("/api/presence/people")
    async def api_people(request: Request, user=Depends(admin_user)):
        body = await body_of(request)
        try:
            clean = presence.validate_people(body)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        save("people.json", clean)
        return clean

    # ---------- rules ----------
    def known_plugs() -> set:
        """Plugs in live data, plus any a saved rule already names (an offline-for-now plug must not block saving the others)."""
        saved = auto.read_rules(settings.plugs_dir / "rules.json") if settings.plugs_dir else []
        return set(auto.plug_rows(settings.collector_dir, settings.plugs_dir)) | {r["then"]["plug"] for r in saved} \
            | {c["plug"] for r in saved for c in r["when"] if c["type"] == "plug"}

    @app.get("/api/automations")
    def api_automations(user=Depends(current_user)):
        st, rules = state(), auto.read_rules(settings.plugs_dir / "rules.json") if settings.plugs_dir else []
        c = ctx(st)
        rise, sset = c.sun(c.wall.date())
        fmt = lambda t: auto.ampm(t.strftime("%H:%M")) if t else None  # noqa: E731
        return {"configured": bool(settings.plugs_dir), "rules": rules,
                "status": {k: {"was": v.get("was"), "last": v.get("last")} for k, v in (st.get("rules") or {}).items() if isinstance(v, dict)},
                "plugs": [{"key": k, "name": p["name"], "online": p["online"], "on": p["on"]} for k, p in c.plugs.items()],
                "people": [{"id": p["id"], "name": p["name"]} for p in people_cfg()["people"]],
                "presence_stale": bool(st.get("stale", True)), "sun": {"sunrise": fmt(rise), "sunset": fmt(sset)}}

    @app.put("/api/automations")
    async def api_automations_put(request: Request, user=Depends(admin_user)):
        body = await body_of(request)
        try:
            rules = auto.validate_rules(body, known_plugs(), {p["id"] for p in people_cfg()["people"]})
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        save("rules.json", {"rules": rules})
        return {"rules": rules}

    @app.post("/api/automations/{rid}/test")
    async def api_automations_test(request: Request, rid: str, user=Depends(admin_user)):
        body = await body_of(request)
        if "rule" in body:   # an unsaved edit from the builder
            try:
                rule = auto.validate_rule(body["rule"], known_plugs(), {p["id"] for p in people_cfg()["people"]})
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
        else:
            rule = next((r for r in auto.read_rules(settings.plugs_dir / "rules.json") if r["id"] == rid), None)
            if rule is None:
                raise HTTPException(status_code=404, detail="No rule with that id.")
        return auto.dry_run_answer(rule, ctx(state()))

    @app.get("/api/automations/log")
    def api_automations_log(limit: int = Query(100), user=Depends(current_user)):
        return {"entries": auto.read_log(settings.plugs_dir / "activity.jsonl", max(1, min(300, limit))) if settings.plugs_dir else []}
