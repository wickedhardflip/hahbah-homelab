from pathlib import Path

from app.caddygen import STRIP_SESSION, render
from app.registry import load_site

REPO = Path(__file__).parents[2]


def test_each_app_gets_a_guarded_route_to_the_host():
    site = {"domain": "hahbah.com", "edge_host": "central", "hosts": {"central": {"ip": "192.168.4.5"}, "tc": {"ip": "192.168.4.6"}},
            "apps": [{"id": "jobs", "subdomain": "jobs", "host": "central", "port": 8080},
                     {"id": "wiki", "subdomain": "wiki", "host": "tc", "port": 3000}]}
    out = render(site)
    assert "@jobs host jobs.hahbah.com" in out and "reverse_proxy host.docker.internal:8080" in out
    assert "reverse_proxy 192.168.4.6:3000" in out              # apps on other machines go by IP
    assert out.count("forward_auth portal:8000") == 2 and out.count("uri /auth/verify") == 2


def test_checked_in_routes_match_apps_yaml():
    expected = render(load_site(REPO / "apps.yaml"))
    actual = (REPO / "hosts/central/caddy/apps.caddy").read_text(encoding="utf-8")
    assert actual == expected, "apps.caddy is out of date: run  python -m app.caddygen ../apps.yaml ../hosts/central/caddy/apps.caddy"


def test_routes_strip_client_identity_headers_and_sso_apps_get_the_secret():
    site = {"domain": "hahbah.com", "edge_host": "central", "hosts": {"central": {"ip": "192.168.4.5"}},
            "apps": [{"id": "jobs", "subdomain": "jobs", "host": "central", "port": 8080, "sso": True},
                     {"id": "plex", "subdomain": "plex", "host": "central", "port": 32400}]}
    out = render(site)
    assert out.count("request_header -Remote-User") == 2 and out.count("request_header -X-Homelab-Proxy") == 2
    jobs, plex = out.split("@plex host")[0], out.split("@plex host")[1]
    assert "header_up X-Homelab-Proxy {env.HOMELAB_PROXY_SECRET}" in jobs
    assert "X-Homelab-Proxy {env" not in plex
    assert jobs.index("request_header -Remote-User") < jobs.index("forward_auth")


def test_route_body_keeps_written_order_so_forward_auth_runs_after_the_strip():
    # Inside `handle`, Caddy sorts directives and runs forward_auth BEFORE request_header,
    # which would delete the Remote-User forward_auth just copied. `route { }` keeps written order.
    site = {"domain": "hahbah.com", "edge_host": "central", "hosts": {"central": {"ip": "192.168.4.5"}},
            "apps": [{"id": "jobs", "subdomain": "jobs", "host": "central", "port": 8080, "sso": True}]}
    out = render(site)
    body = out.split("handle @jobs {")[1]
    assert body.lstrip().startswith("route {")
    route = body.split("route {")[1]
    assert route.index("request_header -Remote-User") < route.index("forward_auth") < route.index("reverse_proxy")


def test_admin_apps_use_the_admin_check_and_a_container_upstream():
    site = {"domain": "hahbah.com", "edge_host": "central", "hosts": {"central": {"ip": "192.168.4.5"}},
            "apps": [{"id": "tautulli", "subdomain": "tautulli", "host": "central", "port": 8181,
                      "upstream": "tautulli:8181", "admin": True},
                     {"id": "jobs", "subdomain": "jobs", "host": "central", "port": 8080}]}
    out = render(site)
    t, j = out.split("@jobs host")[0], out.split("@jobs host")[1]
    assert "uri /auth/verify?role=admin" in t and "reverse_proxy tautulli:8181" in t
    assert "role=admin" not in j and "reverse_proxy host.docker.internal:8080" in j


def test_every_route_strips_the_portal_session_cookie_before_the_app():
    out = (Path(__file__).parents[2] / "hosts" / "central" / "caddy" / "apps.caddy").read_text(encoding="utf-8")
    routes = out.count("reverse_proxy ")
    assert routes and out.count(STRIP_SESSION) == routes


def test_session_strip_catches_every_copy_of_the_cookie():
    import re
    pattern, repl = re.findall(r'header_up Cookie "([^"]+)" "([^"]*)"', STRIP_SESSION)[0]
    strip = lambda c: re.sub(pattern, repl.replace("$1", chr(92) + "1"), c)
    for cookie in ("hl_session=FAKE; hl_session=REAL", "a=1; hl_session=X; hl_session=Y; b=2",
                   "hl_session =REAL", "a=1;	hl_session=REAL", "hl_session=A;hl_session=B;hl_session=C"):
        assert "REAL" not in strip(cookie) and "hl_session" not in strip(cookie), cookie
    assert "a=1" in strip("a=1; hl_session=X; b=2") and "b=2" in strip("a=1; hl_session=X; b=2")


def test_checked_in_firewall_rules_match_apps_yaml():
    from app.firewallgen import render as render_fw
    root = Path(__file__).parents[2]
    expected = render_fw(load_site(root / "apps.yaml"))
    actual = (root / "hosts" / "central" / "firewall-apps.conf").read_text(encoding="utf-8")
    assert actual == expected, "firewall-apps.conf is out of date: run  python -m app.firewallgen ../apps.yaml ../hosts/central/firewall-apps.conf"
    assert "port 8080 proto tcp" in actual and "tautulli" not in actual   # container apps need no host rule


def test_sso_and_admin_apps_refuse_cross_site_writes_but_plex_does_not():
    root = Path(__file__).parents[2]
    out = render(load_site(root / "apps.yaml"))
    for app in ("jobs", "social", "replexon", "tautulli"):
        block = out.split(f"handle @{app} ")[1].split("\nhandle @")[0]
        assert f"respond @xsite_{app} 403" in block, app
        rule = out.split(f"@xsite_{app} expression ")[1].split("\n")[0]
        assert f"'https://{app}.hahbah.com'" in rule and "{header.Origin} != ''" in rule and "'POST'" in rule
    assert "@xsite_plex" not in out


def test_every_route_blanks_a_set_cookie_for_the_portal_session():
    out = (Path(__file__).parents[2] / "hosts" / "central" / "caddy" / "apps.caddy").read_text(encoding="utf-8")
    from app.caddygen import STRIP_SET_SESSION
    assert out.count(STRIP_SET_SESSION) == out.count("reverse_proxy ") > 0
    assert "header_down -Set-Cookie" not in out      # that would delete the apps' own cookies too


def test_set_cookie_strip_hits_only_the_portal_session():
    import re
    from app.caddygen import STRIP_SET_SESSION
    pattern, repl = re.findall(r'header_down Set-Cookie "([^"]+)" "([^"]*)"', STRIP_SET_SESSION)[0]
    strip = lambda v: re.sub(pattern, repl, v)
    for v in ("hl_session=FAKE; Domain=.hahbah.com; Path=/", "hl_session=; Max-Age=0", "hl_session =x"):
        assert strip(v) == "", v
    for v in ("session=abc; Path=/", "app_hl_session=1", "csrftoken=x; Secure"):
        assert strip(v) == v, v


def test_home_route_compresses_text():
    caddyfile = (Path(__file__).parents[2] / "hosts" / "central" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    home = caddyfile.split("handle @home {")[1].split("\n\t}\n")[0]
    assert "encode zstd gzip" in home and home.index("encode") < home.index("reverse_proxy portal:8000")
