#!/usr/bin/env bash
# The email allowlist: ALERT_EMAIL_TO=a@x,b@y in alerts.env (first = primary). The portal's Settings page picks which of
# these get alerts and the morning report; it can never add an address. Prints only "done", a count or a reason.
#   bash /srv/homelab/repo/deploy/alerts-env.sh add <address>
#   bash /srv/homelab/repo/deploy/alerts-env.sh remove <address>
#   bash /srv/homelab/repo/deploy/alerts-env.sh count
# Then: cd /srv/homelab/repo/hosts/central && docker compose up -d --force-recreate portal collector
set -euo pipefail
OUT=/srv/homelab/secrets/alerts.env
umask 077

current() { [ -r "$OUT" ] && sed -n 's/^ALERT_EMAIL_TO=//p' "$OUT" | head -1 | tr -d '\r"'"'"' ' || true; }
count() { current | tr ',' '\n' | grep -c . || true; }

case "${1:-}" in
  add|remove)
    addr=$(printf '%s' "${2:-}" | tr 'A-Z' 'a-z' | tr -d ' ')
    [[ "$addr" =~ ^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$ ]] || { echo "that doesn't look like an email address"; exit 1; }
    list=$(current | tr ',' '\n' | grep . | grep -vxF "$addr" || true)
    [ "$1" = add ] && list=$(printf '%s\n%s\n' "$list" "$addr")
    joined=$(printf '%s\n' "$list" | grep . | paste -sd, - || true)
    [ -n "$joined" ] || { echo "refusing to remove the last address"; exit 1; }
    printf 'ALERT_EMAIL_TO=%s\n' "$joined" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
    unset addr list joined
    echo "done: $(count) address(es) on the list; recreate portal + collector to apply" ;;
  count) echo "$(count) address(es) on the list" ;;
  *) echo "usage: alerts-env.sh add|remove <address> | count"; exit 1 ;;
esac
