"""Slice A: /incidents history page (paging, filters, auth, Discreet, non-admin scrubbing)."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import settingsstore
from app.models import Incident, User, utcnow
from conftest import make_user, sign_in


def seed(app, rows):
    with app.state.SessionLocal() as db:
        for r in rows:
            db.add(Incident(**{"key": f"{r.get('kind', 'app_down')}:{r.get('target', 'plex')}", "kind": "app_down", "target": "plex",
                               "severity": "crit", "message": "Plex is down", **r}))
        db.commit()


def user_client(app, admin=False):
    make_user(app)
    with app.state.SessionLocal() as db:
        db.query(User).update({"is_admin": admin})
        db.commit()
    c = TestClient(app)
    sign_in(c)
    return c


def test_needs_sign_in(client):
    assert client.get("/incidents", follow_redirects=False).status_code == 303


def test_lists_newest_first_with_columns(app):
    now = utcnow()
    seed(app, [{"message": "older thing", "opened_at": now - timedelta(hours=5), "closed_at": now - timedelta(hours=4), "emailed": True, "acked_by": "wife"},
               {"message": "newer thing", "opened_at": now - timedelta(minutes=5), "severity": "warn"}])
    html = user_client(app).get("/incidents").text
    assert html.index("newer thing") < html.index("older thing")
    assert "1 h" in html and "open" in html and "wife" in html and "Yes" in html


def test_paging_25_per_page(app):
    now = utcnow()
    seed(app, [{"message": f"inc-{i:02d}", "opened_at": now - timedelta(minutes=i)} for i in range(30)])
    c = user_client(app)
    p1, p2 = c.get("/incidents").text, c.get("/incidents?page=2").text
    assert "inc-00" in p1 and "inc-24" in p1 and "inc-25" not in p1
    assert "inc-25" in p2 and "inc-29" in p2 and "inc-00" not in p2
    assert c.get("/incidents?page=99").status_code == 200 and c.get("/incidents?page=x").status_code == 200


def test_filters(app):
    now = utcnow()
    seed(app, [{"message": "warn-open", "severity": "warn", "opened_at": now},
               {"message": "crit-closed", "opened_at": now - timedelta(hours=2), "closed_at": now - timedelta(hours=1)}])
    c = user_client(app)
    assert "warn-open" in c.get("/incidents?severity=warn").text and "crit-closed" not in c.get("/incidents?severity=warn").text
    assert "crit-closed" in c.get("/incidents?state=closed").text and "warn-open" not in c.get("/incidents?state=closed").text
    assert "warn-open" in c.get("/incidents?state=open").text and "crit-closed" not in c.get("/incidents?state=open").text
    assert "warn-open" in c.get("/incidents?severity=bogus").text   # unknown filter value = no filter


def test_non_admin_scrubbed(app):
    seed(app, [{"kind": "deploy_failed", "target": "deploy", "message": "Deploy failed: secret commit subject"},
               {"message": "Plex is down"}])
    html = user_client(app).get("/incidents").text
    assert "secret commit subject" not in html and "The last deploy failed." in html and "Plex is down" in html


def test_admin_sees_subjects(app):
    seed(app, [{"kind": "deploy_failed", "target": "deploy", "message": "Deploy failed: secret commit subject"}])
    assert "secret commit subject" in user_client(app, admin=True).get("/incidents").text


def test_discreet_drops_hidden_app(app):
    seed(app, [{"target": "jobs", "message": "Example App is down"}, {"message": "Plex is down"}])
    with app.state.SessionLocal() as db:
        settingsstore.put(db, "discreet", "1")
    html = user_client(app, admin=True).get("/incidents").text
    assert "Plex is down" in html and "Example App" not in html


def test_links_from_footer_and_menu(app):
    c = user_client(app)
    assert 'href="/incidents"' in c.get("/").text and 'href="/incidents"' in c.get("/profile").text


def test_unicode_digit_page_is_not_a_500(app):
    from conftest import make_user, sign_in
    from fastapi.testclient import TestClient
    make_user(app)
    c = TestClient(app)
    sign_in(c)
    assert c.get("/incidents?page=²").status_code == 200
