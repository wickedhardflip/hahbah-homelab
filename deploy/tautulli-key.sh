#!/usr/bin/env bash
# Copy Tautulli's API key into the portal's secrets file. Prints only "done" or a reason, never the key.
#   bash /srv/homelab/repo/deploy/tautulli-key.sh && (cd /srv/homelab/repo && docker compose -f hosts/central/compose.yml --project-name homelab up -d portal)
set -euo pipefail
CONF=/srv/homelab/tautulli/config.ini
OUT=/srv/homelab/secrets/tautulli.env
[ -r "$CONF" ] || { echo "Tautulli isn't set up yet ($CONF missing)"; exit 1; }
key=$(sed -n 's/^api_key[[:space:]]*=[[:space:]]*//p' "$CONF" | head -1 | tr -d '\r')
[ -n "$key" ] || { echo "Tautulli has no API key yet: turn on the API in Tautulli > Settings > Web Interface"; exit 1; }
umask 077
printf 'TAUTULLI_API_KEY=%s\n' "$key" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
unset key
echo done
