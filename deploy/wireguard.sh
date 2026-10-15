#!/usr/bin/env bash
# Set up WireGuard (wg0), the DDNS updater and the status writer from <host dir>/wireguard/. Run by pull.sh on every deploy; safe to repeat.
#   bash deploy/wireguard.sh /srv/homelab/repo/hosts/central
# The server private key is made on this host at /etc/wireguard/server.key (0600). It is never printed, never in the repo,
# and never in wg0.conf: wg-quick loads it from the file with PostUp.
set -euo pipefail

DIR=$1/wireguard
[ -f "$DIR/wg.settings" ] || exit 0   # this host has no WireGuard config (not named .env: those are gitignored)

# shellcheck disable=SC1091
. "$DIR/wg.settings"
[[ $WG_PORT =~ ^[0-9]{1,5}$ ]] || { echo "wireguard: bad WG_PORT" >&2; exit 1; }

command -v wg >/dev/null || { sudo apt-get install -y -qq wireguard-tools >/dev/null; echo "wireguard: installed wireguard-tools"; }

sudo install -d -m 0700 /etc/wireguard /srv/homelab/wireguard
sudo test -f /etc/wireguard/server.key || sudo sh -c 'umask 077; wg genkey > /etc/wireguard/server.key'

# Peers are data, never shell: name = word, key = base64 public key, ip = 10.8.0.x.
conf=$'[Interface]\nAddress = 10.8.0.1/24\nListenPort = '"$WG_PORT"$'\nPostUp = wg set %i private-key /etc/wireguard/server.key\n'
while read -r name key ip _; do
  if [ -z "${name:-}" ] || [[ $name == \#* ]]; then continue; fi
  [[ $name =~ ^[A-Za-z0-9_-]+$ && $key =~ ^[A-Za-z0-9+/]{43}=$ && $ip =~ ^10\.8\.0\.([2-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-4])$ ]] \
    || { echo "wireguard: refusing malformed peer line for '${name:-?}'" >&2; exit 1; }
  conf+=$'\n'"# peer: $name"$'\n[Peer]\nPublicKey = '"$key"$'\nAllowedIPs = '"$ip"$'/32\n'
done < "$DIR/peers.conf"

conf=${conf%$'\n'}   # $(...) below drops the trailing newline, so compare without it or every deploy would restart the tunnel
if [ "$conf" != "$(sudo cat /etc/wireguard/wg0.conf 2>/dev/null || true)" ]; then
  sudo sh -c 'umask 077; cat > /etc/wireguard/wg0.conf' <<<"$conf"
  sudo systemctl enable wg-quick@wg0 >/dev/null 2>&1
  sudo systemctl restart wg-quick@wg0
  echo "wireguard: wg0 config updated"
fi
sudo systemctl is-active --quiet wg-quick@wg0 || sudo systemctl start wg-quick@wg0

# systemd units kept in the repo next to the config
changed=
for u in wg-debug.service ddns.service ddns.timer wgstatus.service wgstatus.timer; do
  [ -f "$DIR/$u" ] || continue
  cmp -s "$DIR/$u" "/etc/systemd/system/$u" || { sudo install -m 0644 "$DIR/$u" "/etc/systemd/system/$u"; changed=1; }
done
[ -z "$changed" ] || sudo systemctl daemon-reload
sudo systemctl enable --now wg-debug.service >/dev/null 2>&1 || true
for t in ddns.timer wgstatus.timer; do [ ! -f "$DIR/$t" ] || sudo systemctl enable --now "$t" >/dev/null 2>&1; done
