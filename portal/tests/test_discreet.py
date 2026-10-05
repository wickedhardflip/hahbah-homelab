import copy
import json
from pathlib import Path

from app.views import apps_for, discreet, snapshot_for

TOPO = json.loads((Path(__file__).parents[1] / "app/topology.json").read_text("utf-8"))


def base():
    s = copy.deepcopy(TOPO)
    s["alerts"] = [{"id": "x", "severity": "crit", "target": "jobs", "kind": "app_down", "message": "Example App is down"},
                   {"id": "y", "severity": "warn", "target": "nas", "kind": "disk_high", "message": "NAS full"}]
    s["edge"]["dns"]["expected"] = [{"name": "home.hahbah.com", "ip": "1", "host": "c"}, {"name": "jobs.hahbah.com", "ip": "1", "host": "c"}]
    s["edge"]["dns"]["wrong"] = ["jobs.hahbah.com"]
    return s


def test_jobs_disappears_everywhere():
    out = discreet(base())
    text = json.dumps(out).lower()
    assert "example app" not in text and "jobs.hahbah.com" not in text and '"jobs"' not in text
    assert [a["id"] for a in out["alerts"]] == ["y"]


def test_green_line_still_runs_central_to_social_through_the_same_bend():
    out = discreet(base())
    links = {l["id"]: l for l in out["links"]}
    green = next(l for l in out["layout"]["lines"] if l["id"] == "green")["links"]
    merged = [links[i] for i in green if i in links and {links[i]["a"], links[i]["b"]} == {"central", "social"}]
    assert len(merged) == 1
    via = out["layout"]["via"][merged[0]["id"]]
    jobs_at = TOPO["layout"]["pos"]["jobs"]
    assert jobs_at in via                           # the line keeps its shape, without a station
    assert all(i in links for i in green)           # every link a line lists still exists


def test_off_by_default_and_original_untouched():
    s = base()
    before = json.dumps(s, sort_keys=True)
    assert snapshot_for(s, True) is s
    snapshot_for(s, True, discreet_mode=True)
    assert json.dumps(s, sort_keys=True) == before


def test_apps_list_hides_jobs():
    apps = [{"id": "jobs"}, {"id": "plex"}]
    assert apps_for(apps, True, discreet_mode=True) == [{"id": "plex"}] and len(apps_for(apps, True)) == 2


def test_dns_text_and_domain_card_never_name_it():
    s = base()
    next(n for n in s["nodes"] if n["id"] == "domain").setdefault("meta", {})["Through the Eero"] =         "Wrong for jobs.hahbah.com, social.hahbah.com"
    s["alerts"] = [{"id": "live-dns", "severity": "warn", "target": "domain", "kind": "dns_drift", "meta": {},
                    "message": "DNS needs a look: jobs.hahbah.com missing; social.hahbah.com points elsewhere."}]
    out = discreet(s)
    blob = json.dumps(out)
    assert "jobs." not in blob
    dom = next(n for n in out["nodes"] if n["id"] == "domain")
    assert dom["meta"]["Through the Eero"] == "Wrong for social.hahbah.com"
    assert out["alerts"][0]["message"] == "DNS needs a look: social.hahbah.com points elsewhere."


def test_commit_subjects_are_admin_only_and_hidden_in_discreet_mode():
    s = base()
    s.setdefault("edge", {})["deploy"] = {"commit": "abc1234", "message": "jobs: tweak scraper", "at": "x", "ok": False}
    s["alerts"] = [{"id": "live-deploy", "severity": "warn", "target": "caddy", "kind": "deploy_failed", "meta": {},
                    "message": "The last deploy failed: jobs: tweak scraper"}]
    for admin, mode in ((False, False), (True, True)):
        blob = json.dumps(snapshot_for(s, admin, mode))
        assert "tweak scraper" not in blob
    assert "tweak scraper" in json.dumps(snapshot_for(s, True, False))


def test_backup_error_text_is_admin_only():
    s = base()
    s["backup"] = {"last_run": {"status": "failure", "error": "rsync: /volume1/secret path denied"}}
    assert "secret path" not in json.dumps(snapshot_for(s, False))
    assert "secret path" in json.dumps(snapshot_for(s, True))
