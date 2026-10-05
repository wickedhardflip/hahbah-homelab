#!/usr/bin/env bash
# Make ufw match <host dir>/firewall.conf + firewall-apps.conf. Run by pull.sh on every deploy.
#   bash deploy/firewall.sh /srv/homelab/repo/hosts/central
# Rules are compared without their comments. New rules go in before stale ones come out,
# so nothing that should be open is ever briefly closed.
set -euo pipefail

DIR=$1
[ -f "$DIR/firewall.conf" ] || exit 0   # this host has no firewall config: leave ufw alone

key() { sed -e "s/ comment '.*'\$//"; }

# Rules are data, never shell: each line must be one of the shapes below, and ufw gets them as an argument array
# (no eval), so a hostile line in the repo can't run anything else through sudo.
ADDR='[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?'
PORT='[0-9]{1,5}(:[0-9]{1,5})?'
BODY="^(allow|deny|limit) (from $ADDR to any port $PORT( proto (tcp|udp))?|$PORT(/(tcp|udp))?|from $ADDR)\$"
COMMENT="^ comment '[A-Za-z0-9 ,.:_>()-]*'\$"
ufw_rule() {   # ufw_rule [delete] "<rule line>"
  local pre=() line body c words
  if [ "$1" = delete ]; then pre=(delete); shift; fi
  line=$1
  body=$(key <<<"$line")
  c=${line#"$body"}
  if ! [[ $body =~ $BODY ]] || { [ -n "$c" ] && ! [[ $c =~ $COMMENT ]]; }; then
    echo "firewall: refusing malformed rule: $line" >&2
    return 1
  fi
  read -ra words <<<"$body"
  if [ -n "$c" ]; then c=${c#" comment '"}; words+=(comment "${c%"'"}"); fi
  [ -n "${CHECK_ONLY:-}" ] && return 0
  sudo ufw "${pre[@]}" "${words[@]}" >/dev/null
}

desired=$(cat "$DIR/firewall.conf" "$DIR/firewall-apps.conf" 2>/dev/null | sed -e 's/[[:space:]]*$//' | grep -vE '^[[:space:]]*(#|$)' || true)
if ! grep -q "port 22 proto tcp" <<<"$desired"; then
  echo "firewall: no SSH rule in $DIR/firewall.conf; refusing to apply" >&2
  exit 1
fi
while IFS= read -r r; do [ -z "$r" ] || CHECK_ONLY=1 ufw_rule "$r" || exit 1; done <<<"$desired"   # validate all before changing any
if [ -n "${CHECK_ONLY:-}" ]; then echo "firewall: rules OK"; exit 0; fi
current=$(sudo ufw show added | sed -n 's/^ufw //p')

declare -A have want
while IFS= read -r r; do [ -n "$r" ] && have["$(key <<<"$r")"]=1; done <<<"$current"
while IFS= read -r r; do [ -n "$r" ] && want["$(key <<<"$r")"]=1; done <<<"$desired"

while IFS= read -r r; do
  if [ -n "$r" ] && [ -z "${have[$(key <<<"$r")]:-}" ]; then
    ufw_rule "$r"
    echo "firewall: added $r"
  fi
done <<<"$desired"
while IFS= read -r r; do
  k=$(key <<<"$r")
  if [ -n "$r" ] && [ -z "${want[$k]:-}" ]; then
    ufw_rule delete "$k"
    echo "firewall: removed $k"
  fi
done <<<"$current"

sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
if ! sudo ufw status | grep -q "^Status: active"; then
  sudo ufw --force enable >/dev/null
  echo "firewall: enabled"
fi
