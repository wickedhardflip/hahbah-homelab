# New app checklist

Steps for adding a systemd app on the central host (example-app, sample-service and RePlexOn are the examples). Each app runs as its own system user, never as `youruser`.

1. **System user.** No login shell, no password, no sudo, no home:
   `sudo useradd --system --no-create-home --home-dir /opt/<app> --shell /usr/sbin/nologin <app>`
2. **Ownership.** `sudo chown -R <app>:<app> /opt/<app>`. Only the app's data dirs need to be writable. If the collector container (uid 1000) must read a file such as a SQLite DB, grant it with group or other read on that path only.
3. **Secrets.** `.env` is `0600`, owned by the app user. Check with `stat`, never print values. Keep it out of git.
4. **Unit and hardening.** Base unit in `/etc/systemd/system/<app>.service`, user in a drop-in `/etc/systemd/system/<app>.service.d/10-user.conf` (`User=`, `Group=`). Keep `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict` (or `full`) with `ReadWritePaths=` for the data dirs, plus `MemoryMax`/`CPUQuota`. Then `daemon-reload` and restart.
5. **apps.yaml.** Add one line: id, subdomain, host, port, `health`, `lan_url`, `sso`, `admin`. The Caddy route and the firewall rules (Docker networks only, so nobody skips SSO via the port) are generated from it. Regenerate with `caddygen` and `firewallgen`; a test fails if you forget.
6. **Health path.** Pick a cheap path that returns 200 without login (e.g. `/login`). The portal checks it.
7. **Single sign-on.** Set `sso: true` so Caddy gates the route and passes the proxy secret. The app should trust that header only from Caddy.
8. **DNS.** The app name must resolve to the edge host (`central`). Confirm `<subdomain>.hahbah.com` resolves on the LAN.
9. **Backups.** Say where the data lives and how it is backed up. Note any NAS (NFS) or root-only access the app needs; if it depends on `youruser`'s sudo or uid, record it here before changing the user.
10. **Monitoring.** Confirm the portal shows the app healthy, `systemctl is-active` is `active`, and `journalctl -u <app> -n 50` is clean.

Verify after any user change: process owner (`ps -o user= -p $(systemctl show -p MainPID --value <app>)`), HTTP 200 on the port, dashboard green, collector data still updating. Keep a rollback copy of the unit and an ownership listing (`find ... -printf '%u:%g %p\n'`) before you start.

## Exceptions

- **RePlexOn** runs as `replexon` with one sudo rule, `/etc/sudoers.d/replexon`: `/usr/bin/crontab -l -u root` (read-only, to show schedules). Schedule editing (`CRON_EDIT_ENABLED`) stays off: a sudo rule for crontab writes would be root. `/etc/replexon` (rsync secret) and `/opt/replexon/.msmtprc` (test emails) are `replexon`-owned `0600`. The manual-backup button runs the root-only `/usr/local/bin/backup-plex.sh` without sudo, so it doesn't work (it didn't as `youruser` either); nightly backups run from root's cron.
- If an app needs root for something, sudo exactly one full command line with fixed arguments (sudo matches arguments exactly), never a script it can edit or a command that writes root's files.
