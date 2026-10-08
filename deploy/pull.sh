#!/usr/bin/env bash
# Pull-based deploy. Run by homelab-deploy.timer every 5 minutes on each host.
# GitHub main is the source of truth: this clone is managed, never edited by hand.
# Each host applies only its own stack: hosts/<host-id>/compose.yml.
set -euo pipefail

ROOT=/srv/homelab
REPO=$ROOT/repo
HOST_ID=$(cat "$ROOT/host-id")
STATE=$ROOT/deployed-sha          # last commit applied successfully
LOG=$ROOT/deploy.log

cd "$REPO"
git fetch --quiet origin main
REMOTE=$(git rev-parse origin/main)
DEPLOYED=$(cat "$STATE" 2>/dev/null || true)
if [ "$REMOTE" = "$DEPLOYED" ] && [ -z "${FORCE:-}" ]; then
  exit 0
fi

# Only deploy commits signed by a key in allowed_signers (root-owned, outside the repo, so a push can't add a key).
# Checking the tip is enough: its hash covers the whole history below it. An unsigned tip is retried (and logged) every run.
if ! git -c gpg.ssh.allowedSignersFile="$ROOT/allowed_signers" verify-commit "$REMOTE" > /dev/null 2>&1; then
  echo "$(date -Is) FAILED signature check at ${REMOTE:0:7} ($(git log -1 --pretty=%s "$REMOTE"))" >> "$LOG"
  exit 1
fi

git reset --hard --quiet "$REMOTE"
# This script may itself have just changed: re-run the new copy so fixes apply on this cycle.
if [ -z "${HOMELAB_REEXEC:-}" ]; then
  HOMELAB_REEXEC=1 exec bash "$REPO/deploy/pull.sh"
fi
MSG=$(git log -1 --pretty=%s)
STACK=$REPO/hosts/$HOST_ID/compose.yml

if [ -f "$STACK" ]; then
  # Validate before touching anything; a bad commit leaves the running stack alone
  # and is retried (and logged) on every run until a fixed commit lands.
  if ! docker compose -f "$STACK" --project-name homelab config --quiet; then
    echo "$(date -Is) FAILED config check at ${REMOTE:0:7} ($MSG)" >> "$LOG"
    exit 1
  fi
  if [ -n "$(docker compose -f "$STACK" --project-name homelab config --services)" ]; then
    # A failed build or start leaves the old containers running; log it so the dashboard shows "Signal failure".
    if ! docker compose -f "$STACK" --project-name homelab up -d --build --remove-orphans > "$LOG.build" 2>&1; then   # only the latest build's output is kept
      echo "$(date -Is) FAILED build at ${REMOTE:0:7} ($MSG)" >> "$LOG"
      exit 1
    fi
    # Config files are bind-mounted, so "up" alone won't notice edits: reload Caddy gracefully.
    # Read the list first: `| grep -q` would exit early, SIGPIPE compose, and under pipefail skip the reload silently.
    SERVICES=$(docker compose -f "$STACK" --project-name homelab config --services)
    if grep -qx caddy <<<"$SERVICES"; then
      if ! docker compose -f "$STACK" --project-name homelab exec -T caddy caddy reload --config /etc/caddy/Caddyfile >> "$LOG.build" 2>&1; then
        echo "$(date -Is) FAILED caddy reload at ${REMOTE:0:7} ($MSG)" >> "$LOG"
        exit 1
      fi
    fi
  else
    # Empty stack (compose refuses "up" with no services): just remove anything left over.
    docker compose -f "$STACK" --project-name homelab down --remove-orphans
  fi
fi

# Firewall: hosts/<host-id>/firewall.conf + firewall-apps.conf (generated from apps.yaml).
if ! bash "$REPO/deploy/firewall.sh" "$REPO/hosts/$HOST_ID" >> "$LOG" 2>&1; then
  echo "$(date -Is) FAILED firewall at ${REMOTE:0:7} ($MSG)" >> "$LOG"
  exit 1
fi

echo "$REMOTE" > "$STATE"
VER=$(tr -d '[:space:]' < "$REPO/VERSION" 2>/dev/null || true)   # release number; Alex says when to bump the major
echo "$(date -Is) deployed ${REMOTE:0:7}${VER:+ v$VER} ($MSG)" >> "$LOG"
