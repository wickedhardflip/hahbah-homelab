# Security

## Reporting a problem

Use GitHub's private vulnerability reporting ("Security" tab, "Report a vulnerability") instead of a public issue. This is a hobby project, so there is no response-time promise, but reports are taken seriously.

## What this repo is designed to do

- No secrets in git: they live in `/srv/homelab/secrets/*.env` (0600) on each host.
- Deploys are pull-based and only signed commits deploy (checked against a root-owned `allowed_signers` file that is outside the repo).
- The web-facing portal holds no registrar, NAS or mail credentials. A separate collector container holds those.
- Apps sit behind one sign-in, with an Origin check on state-changing requests and per-app system users.

## Before you copy this: hardening you should do yourself

This is a home-LAN project that favours convenience in places. A careful reviewer would ask you to tighten these in your own setup:

- **Host privileges.** Use a dedicated deploy user. Avoid passwordless sudo and `docker` group membership for the account you log in with (the group is effectively root).
- **SSH.** Keys only; disable password login.
- **Branch protection.** This repo does not enforce it for your fork. Turn it on, and keep each host's deploy key read-only.
- **Network ranges.** The portal trusts `X-Forwarded-*` only from Caddy's fixed address. If you change the ranges in `hosts/central/compose.yml`, change `FORWARDED_ALLOW_IPS` and the firewall rules with them.
- **Not for the open internet.** It is built for a LAN with a wildcard certificate and local DNS. Do not port-forward it without your own review.
- **No formal audit.** Single maintainer, hobby project.
