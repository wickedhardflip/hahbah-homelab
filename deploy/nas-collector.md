# NAS stats key for the collector container

The `collector` service reads NAS stats over SSH with its own key. The NAS only lets that key run one read-only
script, so the key can't open a shell, forward ports or change anything. Set up 2026-10-01.

**On central** (key + pinned NAS host key, both in `/srv/homelab/secrets`, never in git):

    cd /srv/homelab/secrets
    ssh-keygen -q -t ed25519 -N "" -C "homelab-collector@central" -f nas_collector     # 0600, owned by youruser (uid 1000 = the container's user)
    ssh-keyscan -t ed25519 192.168.4.4 > nas_known_hosts                                 # compare: ssh-keygen -lf nas_known_hosts
    mkdir -p /srv/homelab/collector                                                      # where the collector writes its JSON

**On the NAS** (as admin, from the PC):

1. `~/bin/homelab-stats.sh` (mode 755) = the `NAS_SCRIPT` string from `portal/app/collectors/nas.py`. It answers two fixed requests:
   `stats` (CPU/memory/RAID/volume/temps, every minute) and `smart` (synodisk SMART for sda–sdd, daily). Nothing else runs.
   Previous version kept as `homelab-stats.sh.bak-phase3`.
2. One line appended to `~/.ssh/authorized_keys` (backup at `authorized_keys.bak-collector`):

       command="/var/services/homes/admin/bin/homelab-stats.sh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA… homelab-collector@central

**Check from central:** `ssh -i nas_collector -o UserKnownHostsFile=nas_known_hosts admin@192.168.4.4 anything` prints the
`==stat==` … `==end==` sections whatever command is asked for.

**Revoke:** delete that line on the NAS (or the `nas_collector` files on central). If `NAS_COMMAND` changes in code,
update `homelab-stats.sh` on the NAS to match.
