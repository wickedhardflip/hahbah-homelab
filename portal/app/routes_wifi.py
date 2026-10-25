"""Wi-Fi page + API (coverage and interference). Mounted by main.create_app with install().

Reads /collector/wifi.json (the collector's history, see wifi.py). Node positions come from the floorplan layout (the Eero nodes
already pinned there); admins also place devices by tapping the map, and may upload a one-time neighbor scan.
Everything private (device names, places, scan) lives in the settings database and is served only to signed-in users, device rows to admins.
"""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request

from . import presence, settingsstore, wifi
from .auth import current_user, same_origin
from .plugs import _frac

FLOORPLAN = "floorplan.jpg"
MAX_PLACES = 200


def install(app, templates) -> None:
    settings, SessionLocal = app.state.settings, app.state.SessionLocal

    def admin_user(user=Depends(current_user)):
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admins only.")
        return user

    def origin(request: Request) -> None:
        if not same_origin(request, settings.home_url):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")

    def stored(key: str, default):
        with SessionLocal() as db:
            raw = settingsstore.get(db, key)
        try:
            return json.loads(raw) if raw else default
        except ValueError:
            return default

    def put(key: str, value) -> None:
        with SessionLocal() as db:
            settingsstore.put(db, key, json.dumps(value))

    def history() -> dict:
        try:
            doc = json.loads((settings.collector_dir / "wifi.json").read_text(encoding="utf-8"))["data"]
            if isinstance(doc, dict):
                return {**wifi.EMPTY, **doc}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return dict(wifi.EMPTY)

    @app.get("/wifi")
    def wifi_page(request: Request, user=Depends(current_user)):
        return templates.TemplateResponse(request, "wifi.html", {"user": user, "is_admin": bool(user.is_admin),
                                                                 "has_image": (settings.media_dir / FLOORPLAN).is_file()})

    @app.get("/api/wifi")
    def api_wifi(user=Depends(current_user)):
        out = wifi.summarize(history(), datetime.now(timezone.utc), ZoneInfo(settings.home_tz), bool(user.is_admin))
        pins = {i["id"]: i for i in (stored("floorplan_layout", {}).get("items") or []) if isinstance(i, dict)}
        for n in out["nodes"]:
            p = pins.get("eero:" + n["slug"])
            n["x"], n["y"] = (p["x"], p["y"]) if p else (None, None)
        scan = stored("wifi_scan", None)
        out["interference"] = wifi.interference(out["nodes"], scan["networks"] if scan else None)
        out["scan"] = {"at": scan["at"], "count": len(scan["networks"])} if scan else None
        if user.is_admin:
            out["places"] = stored("wifi_places", {})
            if scan:
                out["scan"]["networks"] = sorted(scan["networks"], key=lambda n: -(n["dbm"] if n["dbm"] is not None else -999))[:8]
        return out

    @app.put("/api/wifi/places")
    async def api_wifi_places(request: Request, user=Depends(admin_user)):
        origin(request)
        try:
            body = await request.json()
            raw = body["places"]
            if not isinstance(raw, dict) or len(raw) > MAX_PLACES:
                raise ValueError
            clean = {}
            for k, v in raw.items():
                if not presence.DEV_RE.fullmatch(k) or not isinstance(v, dict):
                    raise ValueError
                clean[k] = {"x": _frac(v.get("x")), "y": _frac(v.get("y"))}
        except (ValueError, KeyError, TypeError):
            raise HTTPException(status_code=422, detail="Send places as {device id: {x, y}} with positions between 0 and 1.")
        put("wifi_places", clean)
        return {"places": clean}

    @app.post("/api/wifi/scan")
    async def api_wifi_scan(request: Request, user=Depends(admin_user)):
        origin(request)
        try:
            body = await request.json()
            nets = wifi.parse_netsh(body["text"]) if isinstance(body.get("text"), str) else wifi.clean_scan(body.get("networks"))
        except (ValueError, AttributeError, TypeError) as e:
            raise HTTPException(status_code=422, detail=str(e) if isinstance(e, ValueError) and str(e) else "That is not a scan I can read.")
        put("wifi_scan", {"at": presence.iso(datetime.now(timezone.utc)), "networks": nets})
        return {"count": len(nets)}

    @app.delete("/api/wifi/scan")
    def api_wifi_scan_clear(request: Request, user=Depends(admin_user)):
        origin(request)
        put("wifi_scan", None)
        return {"count": 0}
