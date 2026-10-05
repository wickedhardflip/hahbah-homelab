# Configuration reference

Every setting is an environment variable, set in `hosts/central/compose.yml` (plain values) or in a file under `/srv/homelab/secrets/` (anything secret). Defaults are in `portal/app/config.py` and `portal/app/sidecar.py`.

## Portal

| Variable | Default | Meaning |
|---|---|---|
| `PORTAL_BASE_DOMAIN` | `hahbah.com` | Your domain. App names are `<subdomain>.<domain>`. |
| `PORTAL_HOME_URL` | `https://home.hahbah.com` | The portal's own address (used for the Origin check and redirects). |
| `PORTAL_COOKIE_DOMAIN` | unset | e.g. `.example.com`, so one sign-in covers every app. |
| `PORTAL_COOKIE_SECURE` | `1` | Keep `1` behind HTTPS. |
| `PORTAL_TZ` | `America/New_York` | Time zone for the digest, backups summary and weather. |
| `PORTAL_SESSION_DAYS` | `30` | Sign-in lifetime. |
| `PORTAL_COLLECT_INTERVAL` | `30` | Seconds between snapshots. |
| `PORTAL_DATA_DIR` | `/app/data` | SQLite database and runtime data. |
| `PORTAL_APPS_FILE` | `/app/apps.yaml` | The app registry. |
| `PORTAL_DISKS` | `/=/app/data` | `label=path,...` of disks to graph. |
| `PORTAL_NIC` | `enp2s0` | Network interface for traffic numbers. |
| `PORTAL_HOST_PROC`, `PORTAL_HOST_SYS` | `/host/proc`, `/host/sys` | Host stats mounts (read-only). |
| `PORTAL_DEPLOY_LOG` | `/host/deploy.log` | Where the last deploy result is read from. |
| `PORTAL_EERO_IGD` | `http://192.168.4.1:1900/igd.xml` | Router UPnP description for WAN stats. Router-specific; harmless if unreachable. |
| `PORTAL_CADDY_HOST`, `PORTAL_HOST_GATEWAY` | `caddy`, `host.docker.internal` | How the portal reaches Caddy and host apps. |
| `PORTAL_OUTBOX` | unset | Folder the portal writes outgoing emails to. Unset = no email. |
| `PORTAL_MAIL_DIR` | unset | Where `recipients.json` is written. Unset = no web-added addresses. |
| `PORTAL_WEATHER_DIR` | unset | Where the weather location file is written. |
| `PORTAL_SPORTS_FILE` | unset | Where the team picks are written. |
| `PORTAL_COLLECTOR_DIR` | `/collector` | The collector's JSON output, read-only. |
| `PORTAL_MEDIA_DIR` | `/app/media` | About-page video and poster (kept out of git). |
| `FORWARDED_ALLOW_IPS` | set in compose | The only address whose `X-Forwarded-*` headers are trusted (Caddy). |
| `ALERT_EMAIL_TO` | unset | Starting list of alert addresses, first = primary. See `deploy/alerts-env.sh`. |
| `OLLAMA_URL` | unset | Optional local LLM box to show on the map. |
| `TAUTULLI_URL`, `TAUTULLI_API_KEY` | unset | Optional Plex activity. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `PORTAL_STATE_SECRET` | unset | Optional Google sign-in. |

## Collector (`python -m app.sidecar`)

Each job runs only if its setting is present.

| Variable | Default | Starts this job |
|---|---|---|
| `NAS_HOST` (+ `NAS_USER`, `NAS_KEY`, `NAS_KNOWN_HOSTS`) | unset | NAS stats every minute, SMART daily |
| `PORKBUN_API_KEY`, `PORKBUN_API_SECRET_KEY` (+ `EDGE_DOMAIN`, `EERO_DNS`) | unset | Domain, DNS, certificate checks every 6 hours |
| `MOUNTS` (+ `MOUNTS_ROOT`) | unset | NFS mount checks |
| `PING_TARGETS` | unset | `name=ip,...` reachability |
| `SPEEDTEST` | `1` | One internet speed test a day (`0` to turn off) |
| `OUTBOX` (+ `ALERT_EMAIL_TO`, `RECIPIENTS_FILE`) | unset | Sends queued emails with `msmtp` |
| `REPLEXON_DB` | unset | Plex backup summary |
| `SPORTS` (+ `SPORTS_PICKS`) | `1` | Stadium scores (MLB Stats API, ESPN public feeds; no keys) |
| `WEATHER_LOCATION` | unset | Forecast from Open-Meteo (no key) |
| `COLLECTOR_DIR` | `/collector` | Where each job writes its JSON |
| `PORTAL_APPS_FILE`, `PORTAL_TZ` | as above | Shared with the portal |

## Settings page (stored in the portal database)

| Setting | Notes |
|---|---|
| Instant alerts, morning report (on/off, time) | Danger emails and all-clears; the report goes out once a day |
| Alert addresses | Add up to 5, remove any, choose the primary; per-address switches for alerts and the report |
| Mute alerts | Temporary, with an expiry |
| Weather place | City or ZIP; resolved by Open-Meteo |
| Sports teams | Ballpark (MLB), arena (NBA/WNBA/NHL), bus stop (NBA/WNBA/NHL/NFL/MLS or none) |
| Idle sign-out | 15 to 10080 minutes |
| Discreet mode | Hides the `jobs` sample app everywhere in the portal |
