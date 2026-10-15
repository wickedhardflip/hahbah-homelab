# Make it your own

A guide for adapting this repo to your own network. Read the main [README](../README.md) first for what the pieces are.

## What you need

| Need | Why | Notes |
|---|---|---|
| A Linux host that is always on | Runs Docker and the stack | Written for Ubuntu on a small PC. Give it a DHCP reservation. |
| Docker Engine + Compose plugin | The stack is `hosts/central/compose.yml` | |
| A domain at **Porkbun** | Caddy gets one wildcard certificate with the Porkbun DNS challenge | Another DNS provider works if you change `hosts/central/caddy/Dockerfile` and the `tls` block in the Caddyfile. |
| Local DNS that points `*.yourdomain` at the host | App names resolve on your LAN | Most routers have a local DNS or hosts feature. |
| A GitHub repo (your fork) | The host pulls and deploys from it | Private is fine. Use a read-only deploy key. |
| An SSH signing key | Only signed commits deploy | Public half goes in `/srv/homelab/allowed_signers` on each host. |

## Optional integrations

None of these is needed to see the portal work. Leave the matching setting unset and that part simply doesn't appear (the collector only starts the jobs whose settings you provide).

| Integration | Turn it on by | What you get |
|---|---|---|
| NAS stats | `NAS_HOST`, `NAS_USER`, `NAS_KEY`, `NAS_KNOWN_HOSTS` and the restricted-key setup in `deploy/nas-collector.md` | NAS CPU, RAM, RAID, volumes, SMART |
| NFS mount checks | `MOUNTS=/mnt/a,/mnt/b` | Mount stations and alarms |
| Pings | `PING_TARGETS=name=ip,...` | Router, NAS and host reachability |
| Domain and DNS checks | `secrets/porkbun.env` | Expiry, auto-renew, DNS drift |
| Eero Wi-Fi and devices | `secrets/eero.env` (token from `tools/eero_login.py`) | Nodes, devices, 24 h / 7 d / 30 d data use. Read-only, uses the unofficial Eero app API |
| Router DNS check | `EERO_DNS` (any resolver that should answer your names) | "Resolves through the router" |
| Plex | A Plex app line in `apps.yaml` | App health. Activity stats need Tautulli. |
| Tautulli | `secrets/tautulli.env` | "N watching", history, newsletter |
| RePlexOn | `REPLEXON_DB` | Nightly Plex backup station ([RePlexOn](https://github.com/wickedhardflip/replexon) is a separate project) |
| Google sign-in | `secrets/google.env` | Sign in with Google next to the password form |
| Email | A configured `msmtp` account, `OUTBOX` | Danger alerts, all-clears, morning report |
| Sports and weather | On by default | Scores on the three stadium spots; set teams and place on the Settings page |

## First deploy, step by step

1. **Fork or copy this repo.** Rename it, then edit `apps.yaml` (start from `apps.example.yaml`): your domain, your host's LAN address, your apps.
2. **Prepare the host.** Install Docker. Create `/srv/homelab/{secrets,collector,outbox,weather,mail,portal/data,caddy}` and `chown` the data folders to uid 1000 (the container user).
3. **Write the secrets** from `secrets.example/` into `/srv/homelab/secrets/` (`chmod 600`). At minimum `porkbun.env` and `proxy.env`. Use the helper scripts in `deploy/` where one exists so values never show on screen.
4. **Set host identity.** `echo central > /srv/homelab/host-id`. Put your signing key's public half in `/srv/homelab/allowed_signers`.
5. **Clone** your repo to `/srv/homelab/repo` with a read-only deploy key, then install the two units in `deploy/` (`homelab-deploy.service` has a `User=` line to set) and enable the timer.
6. **Generate the app routes.** From `portal/`, run `python -m app.caddygen ../apps.yaml ../hosts/central/caddy/apps.caddy` and `python -m app.firewallgen ../apps.yaml ../hosts/central/firewall-apps.conf`, commit, and push a **signed** commit.
7. **Wait up to 5 minutes**, or run `sudo systemctl start homelab-deploy.service`. Watch `/srv/homelab/deploy.log`.
8. **Create your first user**: `docker exec -it homelab-portal-1 python -m app.manage create-user <name>` (it prompts for a password), then `set-admin <name>`.
9. Open `https://home.<yourdomain>`.

## Replace these placeholders

The repo ships with documentation-style values. Find and replace them with yours (a project-wide search for the left column finds every one):

| Placeholder | Replace with | Where it lives |
|---|---|---|
| `example.com` | your domain | `apps.yaml` (`domain:`), `hosts/central/caddy/Caddyfile` and `apps.caddy` (`*.example.com`, `home.example.com`), `hosts/central/compose.yml`, `hosts/central/caddy/Dockerfile`, `portal/app/topology.json`, `portal/app/config.py`, `portal/app/emails.py`, `portal/app/templates/` (`about.html`, `dashboard.html`), `docs/` |
| `192.168.1.x` (hosts `.1` router, `.4`, `.5` central, `.6`, `.9`) | your LAN subnet, keeping or changing the last number per device | `apps.yaml` (`hosts:` and `lan_url`), `hosts/central/compose.yml` (NAS, router, `OLLAMA_URL`), `hosts/central/firewall*.conf` (the `/22` LAN range), `portal/app/topology.json` |
| `youruser` | your Linux login | `deploy/homelab-deploy.service` (`User=`) |
| `enp2s0`, `PORTAL_DISKS`, `PORTAL_TZ` | your NIC, disks and timezone | `hosts/central/compose.yml` |
| `Cambridge, MA` | your town (weather) | Settings page after sign-in |
| `portal/tests/fixtures/` | leave as is | test data; it uses the same placeholders and does not need changing |

After editing `apps.yaml`, regenerate the routes and firewall file (step 6 above). The drawn map (`portal/app/topology.json`), the project story (`portal/app/templates/about.html`) and the photos in `portal/app/static/` are the original author's artwork; swap them when you want it to feel like yours.

## Rebranding

"HAHBAH" is Boston for "harbor", which is why the whole thing is Boston-themed (subway lines, stadium spots). Change the name in `portal/app/templates/` (`login.html`, `about.html`, `dashboard.html` header), the sign in `static/site/hahbah-sign.svg`, and the favicons. The three stadium spots follow whatever teams you pick on the Settings page, and the 3D map's town layout is in `dashboard.html` (`hand-drawn Boston`).

## Operating notes

- **Deploys:** push a signed commit to `main`; hosts pull every 5 minutes. A failed signature, config check, build or firewall step is logged and retried; the running stack is left alone.
- **Logs:** `/srv/homelab/deploy.log` (deploys), `docker compose -f hosts/central/compose.yml logs portal collector`.
- **Backups:** the portal database and Caddy's certificates live under `/srv/homelab`; back that folder up.
- **Updating Caddy or Tautulli:** versions and digests are pinned. Bump them deliberately.

## FAQ

**Do I need a NAS, Plex or an Eero?** No. The collector starts only the checks you configure. Without a NAS you simply get no NAS station data.

**Do I need Porkbun?** For the stock Caddy setup, yes (certificate and domain checks). Swapping the DNS plugin is a small Caddy change.

**Can I run it without a domain?** Not as shipped: the app names, cookies and certificate all assume one.

**Why are deploys pull-based and signed?** So nothing outside the LAN can reach in, and a stolen GitHub login alone can't ship code to the host.

**Where do I change the sports teams or weather place?** Settings page, after you sign in as an admin.

**Is this safe to expose to the internet?** Not without your own review. See [SECURITY.md](../SECURITY.md).
