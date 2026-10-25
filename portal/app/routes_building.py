"""/building: an architect's presentation model of the building (Three.js, see static/building.js). Mounted by main.create_app with install().

The address, footprint and level count are private, so they are not in git: an optional building.json in the media folder
overrides the placeholder below (3 stories, long garden block). Same for the floorplan drawing, served by the existing /media route.
"""
import json

from fastapi import Depends, Request

from .auth import current_user

FLOORPLAN = "floorplan.jpg"
# Units are feet-ish model units, x east and y south. unit = [x0, y0, x1, y1] on the unit level's plate.
PLACEHOLDER = {"caption": "Building model", "placeholder": True, "levels": 3, "unit_level": 2,
               "footprint": [[0, 0], [56, 0], [56, 20], [32, 20], [32, 16.5], [24, 16.5], [24, 20], [0, 20]],
               "unit": [32.6, 0.6, 55.4, 19.4]}


def _pt(p) -> bool:
    return isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1000 for v in p)


def clean(doc) -> dict:
    """The stored building.json if its shape is sound, else the placeholder. Never raises."""
    try:
        fp, unit, n, ul = doc["footprint"], doc["unit"], doc["levels"], doc["unit_level"]
        if (isinstance(fp, list) and 3 <= len(fp) <= 60 and all(_pt(p) for p in fp) and isinstance(unit, list) and len(unit) == 4
                and all(isinstance(v, (int, float)) for v in unit) and isinstance(n, int) and 1 <= n <= 6 and isinstance(ul, int) and 1 <= ul <= n):
            cap = doc.get("caption")
            return {"caption": cap[:80] if isinstance(cap, str) and cap.isprintable() else "Building model", "placeholder": False,
                    "levels": n, "unit_level": ul, "footprint": fp, "unit": unit}
    except (KeyError, TypeError):
        pass
    return dict(PLACEHOLDER)


def install(app, templates) -> None:
    settings = app.state.settings

    @app.get("/building")
    def building_page(request: Request, user=Depends(current_user)):
        return templates.TemplateResponse(request, "building.html", {"user": user, "has_image": (settings.media_dir / FLOORPLAN).is_file()})

    @app.get("/api/building")
    def api_building(user=Depends(current_user)):
        try:
            doc = json.loads((settings.media_dir / "building.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        return {**clean(doc), "has_image": (settings.media_dir / FLOORPLAN).is_file()}
