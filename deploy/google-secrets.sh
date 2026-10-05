#!/usr/bin/env bash
# Write the portal's Google sign-in secrets. Prompts are hidden; prints only "done" or a reason, never a value.
#   bash /srv/homelab/repo/deploy/google-secrets.sh && (cd /srv/homelab/repo && docker compose -f hosts/central/compose.yml --project-name homelab up -d portal)
set -euo pipefail
OUT=/srv/homelab/secrets/google.env
read -r -p "Google client ID (ends in .apps.googleusercontent.com): " id
case "$id" in *.apps.googleusercontent.com) ;; *) echo "That doesn't look like a Google client ID. Nothing changed."; exit 1 ;; esac
read -r -s -p "Google client secret (hidden): " secret; echo
[ -n "$secret" ] || { echo "Empty secret. Nothing changed."; exit 1; }
state=$(head -c 32 /dev/urandom | base64 | tr -d '\n=+/')
umask 077
printf 'GOOGLE_CLIENT_ID=%s\nGOOGLE_CLIENT_SECRET=%s\nPORTAL_STATE_SECRET=%s\n' "$id" "$secret" "$state" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
unset secret state
echo done
