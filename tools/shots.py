"""Take the README screenshots from a running sample portal (tools/sample_portal.py).

    python tools/shots.py OUT_DIR [--url http://127.0.0.1:8765]

Headless Chromium (Playwright) with software WebGL so the 3D view renders. Saves PNG/JPG files into OUT_DIR.
"""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

USER, PASSWORD = "demo", "sample-demo-password"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        ctx = b.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(a.url + "/login")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "login.jpg"), type="jpeg", quality=82)
        page.fill("input[name=username]", USER)
        page.fill("input[name=password]", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url(a.url + "/", timeout=15000)
        page.wait_for_timeout(2500)

        page.keyboard.press("1")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "map-day.png"))
        page.keyboard.press("3")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "noc.png"))
        page.keyboard.press("2")
        page.wait_for_timeout(7000)   # the 3D scene folds up from top-down
        page.screenshot(path=str(out / "3d.jpg"), type="jpeg", quality=85)
        page.keyboard.press("1")
        page.wait_for_timeout(800)
        page.get_by_role("button", name="Midnight").click()
        page.wait_for_timeout(1200)
        page.screenshot(path=str(out / "map-midnight.png"))
        page.goto(a.url + "/settings")
        page.wait_for_timeout(1500)
        page.set_viewport_size({"width": 1600, "height": 1500})
        page.wait_for_timeout(800)
        page.screenshot(path=str(out / "settings.jpg"), type="jpeg", quality=85, animations="disabled")
        b.close()
        print("page errors:", errors or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
