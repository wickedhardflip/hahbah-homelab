"""Render the firewall rules that let Caddy reach each app on the edge host. Output is checked in
(hosts/<edge>/firewall-apps.conf) and applied by deploy/firewall.sh next to the hand-written firewall.conf.

    python -m app.firewallgen ../apps.yaml ../hosts/central/firewall-apps.conf

Apps listen on the host, but only Docker's networks (where Caddy runs) may reach them, so nobody on the LAN
can skip single sign-on by going straight to the port. Apps with an `upstream` run inside the stack and need no rule.
"""
import sys
from pathlib import Path

from .registry import load_site

HEADER = "# Generated from apps.yaml by `python -m app.firewallgen`. Do not edit by hand.\n"
DOCKER_NETS = "172.16.0.0/12"


def render(site: dict) -> str:
    out = [HEADER]
    for app in site["apps"]:
        if app["host"] == site["edge_host"] and not app.get("upstream"):
            out.append(f"allow from {DOCKER_NETS} to any port {app['port']} proto tcp comment 'caddy -> {app['id']}'\n")
    return "".join(out)


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.write_text(render(load_site(src)), encoding="utf-8", newline="\n")
    print(f"wrote {dst}")
