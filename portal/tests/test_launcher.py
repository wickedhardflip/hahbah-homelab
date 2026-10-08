"""Slice B: header app launcher (admin vs non-admin, Discreet, link URLs, status dots)."""
import re

from fastapi.testclient import TestClient

from app import settingsstore
from app.models import User
from conftest import make_user, sign_in

APPS = """domain: hahbah.com
edge_host: central
hosts:
  central: {ip: 192.168.4.5}
apps:
  - {id: jobs, name: Example App, subdomain: jobs, host: central, port: 8080, health: /, lan_url: "http://x:1", admin: true}
  - {id: social, name: Sample Service, subdomain: social, host: central, port: 4434, health: /, lan_url: "http://x:2", admin: true}
  - {id: plex, name: Plex, subdomain: plex, host: central, port: 32400, health: /, lan_url: "http://x:3"}
"""


def launcher(app, settings, admin=False, discreet=False):
    settings.apps_file.write_text(APPS)
    make_user(app)
    with app.state.SessionLocal() as db:
        db.query(User).update({"is_admin": admin})
        if discreet:
            settingsstore.put(db, "discreet", "1")
        db.commit()
    c = TestClient(app)
    sign_in(c)
    html = c.get("/profile").text
    return html, re.findall(r'<a class="app"[^>]*href="([^"]+)"', html)


def test_admin_sees_every_app_with_subdomain_urls(app, settings):
    html, links = launcher(app, settings, admin=True)
    assert links == ["https://jobs.hahbah.com", "https://social.hahbah.com", "https://plex.hahbah.com"]
    assert 'aria-label="Apps"' in html


def test_non_admin_skips_admin_apps(app, settings):
    _, links = launcher(app, settings)
    assert links == ["https://plex.hahbah.com"]


def test_discreet_hides_job_scraper(app, settings):
    html, links = launcher(app, settings, admin=True, discreet=True)
    assert links == ["https://social.hahbah.com", "https://plex.hahbah.com"] and "Example App" not in html


def test_status_dot_from_snapshot(app, settings, builder):
    builder.snapshot = {**builder.snapshot, "nodes": [{"id": "plex", "status": "good"}, {"id": "social", "status": "crit"}]}
    app.state.store.refresh()
    html, _ = launcher(app, settings, admin=True)
    assert re.search(r'class="app"[^>]*href="https://plex[^"]*"[^>]*>\s*<i class="dot up"', html)
    assert re.search(r'href="https://social[^"]*"[^>]*>\s*<i class="dot down"', html)
    assert re.search(r'href="https://jobs[^"]*"[^>]*>\s*<i class="dot unknown"', html)
