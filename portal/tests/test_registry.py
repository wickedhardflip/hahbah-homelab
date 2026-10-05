import pytest

from app.registry import load_registry


def test_loads_the_real_apps_file():
    from pathlib import Path
    apps = load_registry(Path(__file__).parents[2] / "apps.yaml")
    assert {a["id"] for a in apps} == {"jobs", "social", "replexon", "plex", "tautulli"}   # LegacyMonitor retired 2026-10-02
    # host-port apps have a LAN URL; container-only apps (Tautulli) are reached through Caddy alone
    assert all(a["lan_url"].startswith("http://192.168.4.5:") or (a["lan_url"] == "" and a.get("upstream")) for a in apps)


def test_missing_file_is_empty(tmp_path):
    assert load_registry(tmp_path / "none.yaml") == []


def test_entry_missing_a_key_names_it(tmp_path):
    f = tmp_path / "apps.yaml"
    f.write_text("apps:\n  - {id: jobs, name: Jobs}\n")
    with pytest.raises(ValueError, match="jobs.*port"):
        load_registry(f)


def test_yaml_syntax_error_becomes_a_clear_value_error(tmp_path):
    f = tmp_path / "apps.yaml"
    f.write_text("apps:\n  - {id: jobs, name: [unclosed\n")
    with pytest.raises(ValueError, match="isn't valid YAML"):
        load_registry(f)


def test_real_site_has_names_and_edge():
    from pathlib import Path
    from app.registry import load_site
    site = load_site(Path(__file__).parents[2] / "apps.yaml")
    assert site["domain"] == "hahbah.com" and site["edge_host"] == "central"
    assert site["hosts"]["central"]["ip"] == "192.168.4.5"
    jobs = next(a for a in site["apps"] if a["id"] == "jobs")
    assert jobs["fqdn"] == "jobs.hahbah.com" and jobs["url"] == "https://jobs.hahbah.com" and jobs["edge_ip"] == "192.168.4.5"


def test_without_domain_urls_fall_back_to_lan(tmp_path):
    f = tmp_path / "apps.yaml"
    f.write_text("apps:\n  - {id: jobs, name: Jobs, subdomain: jobs, host: central, port: 8080, health: /, lan_url: 'http://192.168.4.5:8080'}\n")
    [app] = load_registry(f)
    assert app["url"] == "http://192.168.4.5:8080" and app["fqdn"] is None


def test_app_on_unknown_host_is_rejected(tmp_path):
    f = tmp_path / "apps.yaml"
    f.write_text("domain: hahbah.com\nedge_host: central\nhosts: {central: {ip: 192.168.4.5}}\n"
                 "apps:\n  - {id: x, name: X, subdomain: x, host: thinkcentre, port: 1, health: /, lan_url: 'http://h:1'}\n")
    with pytest.raises(ValueError, match="x.*thinkcentre"):
        load_registry(f)


def test_lan_url_may_be_empty_only_with_an_upstream(tmp_path):
    import pytest
    from app.registry import load_site
    p = tmp_path / "apps.yaml"
    base = "  - {id: t, name: T, subdomain: t, host: central, port: 8181, health: /status, lan_url: ''"
    p.write_text("domain: hahbah.com\nedge_host: central\nhosts: {central: {ip: 192.168.4.5}}\napps:\n" + base + "}\n")
    with pytest.raises(ValueError, match="no lan_url and no upstream"):
        load_site(p)
    p.write_text("domain: hahbah.com\nedge_host: central\nhosts: {central: {ip: 192.168.4.5}}\napps:\n"
                 + base + ", upstream: 'tautulli:8181', admin: true}\n")
    assert load_site(p)["apps"][0]["url"] == "https://t.hahbah.com"


def test_registry_is_cached_by_mtime_and_callers_get_copies(tmp_path):
    import os
    from app import registry
    p = tmp_path / "apps.yaml"
    body = ("domain: hahbah.com\nedge_host: c\nhosts: {c: {ip: 1.2.3.4}}\napps:\n"
            "  - {id: a, name: A, subdomain: a, host: c, port: 1, health: x, lan_url: 'http://a'}\n")
    p.write_text(body, encoding="utf-8")
    first = registry.load_registry(p)
    first[0]["name"] = "mutated"
    assert registry.load_registry(p)[0]["name"] == "A"
    calls = []
    real = registry._parse_site
    registry._parse_site = lambda path: (calls.append(1), real(path))[1]
    try:
        registry.load_registry(p)
        assert not calls                                  # unchanged file: no re-parse
        p.write_text(body.replace("name: A", "name: B"), encoding="utf-8")
        os.utime(p, ns=(1, 1))                            # force a different mtime
        assert registry.load_registry(p)[0]["name"] == "B" and calls
    finally:
        registry._parse_site = real
