# WireGuard VPN (Ticket Gate)

Live since 2026-10-12. Lets Alex's phone reach the HAHBAH portal and apps from anywhere. Design and reasoning: `docs/specs/2026-10-12-wireguard-vpn-design.md`.

## How it works

```
phone (10.8.0.2)  --UDP 51820-->  Eero (port forward)  -->  Central 192.168.1.5  (wg0 = 10.8.0.1)
                                                                    |
                                                          Caddy 443 -> portal + apps (*.example.com)
```

- Self-hosted WireGuard on Central (the mini PC). No Tailscale or other third party in the path.
- The Eero forwards one UDP port (51820) to 192.168.1.5. That is the only port this adds to the internet.
- Endpoint name: `ticket.example.com`, an A record kept equal to the home public IP by a timer (the IP can change).
- The phone's `AllowedIPs` is `192.168.1.5/32`: only traffic for Central goes through the tunnel. On the server the peer is `10.8.0.2/32`.
- **No IP forwarding** on the server. Packets for 192.168.1.5 arriving on wg0 are handled locally, so the phone cannot reach the NAS, the Eero or other LAN devices. The existing `*.example.com` records (which point at 192.168.1.5) work unchanged over the tunnel.
- SSH stays LAN-only (ufw), so a VPN peer cannot SSH in. Portal sign-in (Google SSO) still applies on top.
- Caddy (80/443) is not reachable from the internet: the Eero forwards only Plex (32400) and WireGuard.

## Files

| Path | What |
|---|---|
| `deploy/wireguard.sh` | Run by `pull.sh` on every deploy, idempotent. Installs `wireguard-tools`, makes the server key, writes `/etc/wireguard/wg0.conf` from `peers.conf`, restarts wg0 only when the config changed, installs the systemd units. |
| `hosts/central/wireguard/wg.settings` | `WG_PORT`. Not named `.env` because `*.env` is gitignored (that mistake cost one deploy). |
| `hosts/central/wireguard/peers.conf` | One line per device: `name publickey 10.8.0.x`. Public keys only. |
| `hosts/central/wireguard/ddns.py` + `.service` + `.timer` | Every 5 min: reads the WAN IP from the Eero (local UPnP), updates `ticket.example.com` at Porkbun only if it differs (max 2 API calls, no retries). Writes `/srv/homelab/collector/ddns.json`. |
| `hosts/central/wireguard/wgstatus.py` + `.service` + `.timer` | Every minute: parses `wg show wg0 dump`, keeps session history (90 days) and "unknown knock" counts (30 days), writes `/srv/homelab/collector/wireguard.json` and `/srv/homelab/wireguard/history.json`. |
| `hosts/central/wireguard/wg-debug.service` | Turns on kernel dynamic debug for WireGuard lines containing "Invalid" so rejected handshakes reach the kernel log. |
| `hosts/central/firewall.conf` | `allow 51820/udp comment 'wireguard'` |
| `portal/app/merge.py` (`apply_vpn`), `portal/app/static/noc.js` (`vpn()`), `portal/app/topology.json` (`ticket`, `vpn1`) | Portal side: Ticket Gate station, NOC "VPN" section, alert rules. |

Secrets: the server private key lives only at `/etc/wireguard/server.key` (0600, root). `wg0.conf` loads it with `PostUp = wg set %i private-key ...`, so no key is in the config, the repo, logs or output. Porkbun keys are read from `/srv/homelab/secrets/porkbun.env` by the DDNS unit only.

## Monitoring and alerts

- NOC view (press `3`), "VPN" section: tunnel card, one card per peer (**CONNECTED** green / **NOT CONNECTED** grey), Dynamic DNS card, recent connections (start, end, length, source IP, data), unknown knocks per day.
- Map: **Ticket Gate** station next to the Eero (green = tunnel up).
- "Connected" means the last handshake is under 180 s old. WireGuard is silent when idle, so an off phone takes up to ~3 min to flip, plus up to ~1-2 min for the portal.
- Alerts (all target the Ticket Gate station): **Danger** tunnel interface down or `wireguard.json` older than 5 min; **Caution** DDNS failing or DNS differs from the WAN IP for over 15 min; **Caution** more than 200 unknown knocks in a day. An idle or disconnected peer never alerts.

## Operations

**Add a device** (e.g. a laptop): in the WireGuard app create a tunnel, let it generate its own key pair, send the public key only. Add a line to `hosts/central/wireguard/peers.conf` (`laptop <public key> 10.8.0.3`), commit and push. In the app: Address `10.8.0.3/32`; peer = server public key (`sudo wg show wg0 public-key`), endpoint `ticket.example.com:51820`, Allowed IPs `192.168.1.5/32`, keepalive 25.

**Remove a device:** delete its line, commit and push. The deploy rewrites wg0.conf and restarts the tunnel.

**Check by hand** (on Central): `sudo wg show wg0`, `systemctl status wg-quick@wg0 ddns.timer wgstatus.timer`, `cat /srv/homelab/collector/ddns.json`, `sudo journalctl -u wgstatus.service -n 20`.

**Change the port:** edit `wg.settings` and the ufw line in `firewall.conf`, push, then change the forward in the Eero app and the endpoint on each device.

**Rollback:** `sudo systemctl disable --now wg-quick@wg0 ddns.timer wgstatus.timer`, remove the Eero forward and the ufw line.

## Gotchas and open items

- Never rename `wg.settings` to `*.env` (gitignored, so the installer silently skips).
- The Eero is read-only for Claude: the UDP forward was made by Alex in the Eero app.
- The wgstatus unit needs `CAP_DAC_OVERRIDE` to write into the collector folder (owned by youruser).
- `wg0.conf` is compared without its trailing newline; otherwise every deploy would restart the tunnel and drop the phone.
- Open: the knock-counter regex (`KNOCK_RE` in `wgstatus.py`) is unverified against real kernel log lines (nothing has knocked yet).
- Open: confirm from the phone on cellular that only Caddy (443) and Plex answer on 192.168.1.5 (Docker-published ports bypass ufw).
- When the apps move to the Proxmox `apps` VM, WireGuard moves with Caddy (the port forward then points at that host).
