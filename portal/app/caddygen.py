"""Render Caddy routes for every app in apps.yaml. Output is checked in (hosts/<edge>/caddy/apps.caddy).

    python -m app.caddygen ../apps.yaml ../hosts/central/caddy/apps.caddy

Every route drops client-sent identity headers, asks the portal (forward_auth) before proxying,
and apps marked `sso: true` also get the shared proxy secret so they can trust Remote-User.
The portal's session cookie is removed before proxying (forward_auth still sees it), so no app ever holds
a token that would sign someone in everywhere, and an app can't set one either (a Set-Cookie for hl_session
is blanked on the way back; the app's other cookies pass untouched).
Apps marked `admin: true` need an admin account (`/auth/verify?role=admin`); `upstream:` overrides the address
Caddy proxies to (e.g. a container on the stack's network).
"""
import sys
from pathlib import Path

from .registry import load_site

HEADER = "# Generated from apps.yaml by `python -m app.caddygen`. Do not edit by hand.\n"
# Removes hl_session=... from the Cookie header the app receives, keeping the app's own cookies. Each match keeps
# only its own leading ";", so back-to-back copies (hl_session=A; hl_session=B) all go; parsers ignore empty pairs.
STRIP_SESSION = '\t\t\theader_up Cookie "(^|;)\\s*hl_session\\s*=[^;]*" "$1"\n'

# Blanks any Set-Cookie that would set or clear hl_session. An empty Set-Cookie header is ignored by browsers;
# a plain `header_down -Set-Cookie` would also destroy every app's own login cookie.
STRIP_SET_SESSION = '\t\t\theader_down Set-Cookie "^\\s*hl_session\\s*=.*" ""\n'


def render(site: dict) -> str:
    out = [HEADER]
    for app in site["apps"]:
        upstream = app.get("upstream") or (f"host.docker.internal:{app['port']}" if app["host"] == site["edge_host"]
                                           else f"{site['hosts'][app['host']]['ip']}:{app['port']}")
        verify = "/auth/verify?role=admin" if app.get("admin") else "/auth/verify"
        secret = "\t\t\theader_up X-Homelab-Proxy {env.HOMELAB_PROXY_SECRET}\n" if app.get("sso") else ""
        proxy = f"\t\treverse_proxy {upstream} {{\n{secret}{STRIP_SESSION}{STRIP_SET_SESSION}\t\t}}\n"
        # A browser write whose Origin is another site (even a sibling *.hahbah.com, which SameSite=Lax treats as
        # same-site) never reaches an SSO or admin app. No Origin = not a browser form/fetch, so it passes.
        # Plex is left out: Plex Web may legitimately be served from app.plex.tv.
        origin = f"https://{app['subdomain']}.{site['domain']}"
        xsite = (f"@xsite_{app['id']} expression {{method}} in ['POST', 'PUT', 'PATCH', 'DELETE'] && "
                 f"{{header.Origin}} != '' && {{header.Origin}} != '{origin}'\n") if app.get("sso") or app.get("admin") else ""
        block = f"\t\trespond @xsite_{app['id']} 403\n" if xsite else ""
        # `route` keeps the written order; in a bare `handle` Caddy runs forward_auth before
        # request_header, so the strip would delete the Remote-User forward_auth just copied.
        out.append(
            f"\n@{app['id']} host {app['subdomain']}.{site['domain']}\n{xsite}"
            f"handle @{app['id']} {{\n\troute {{\n{block}"
            f"\t\trequest_header -Remote-User\n\t\trequest_header -X-Homelab-Proxy\n"
            f"\t\tforward_auth portal:8000 {{\n\t\t\turi {verify}\n\t\t\tcopy_headers Remote-User\n\t\t}}\n"
            f"{proxy}"
            f"\t}}\n}}\n")
    return "".join(out)


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.write_text(render(load_site(src)), encoding="utf-8", newline="\n")
    print(f"wrote {dst}")
