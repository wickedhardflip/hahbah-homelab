# Contributing

This started as one person's homelab and is shared as a template to learn from and make your own. Issues and pull requests are welcome, with one expectation: it is a hobby project, so replies may take a while.

## Good first contributions

- Make an optional piece truly optional (see "Known rough edges" in the README).
- Support another DNS provider for the wildcard certificate (today: Porkbun).
- Fix docs where a first-time setup tripped you up. That is the most useful kind of report.

## Before you open a pull request

```
cd portal
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest -q
```

The suite must pass. Keep changes small and match the surrounding style. If you add a setting or environment variable, add it to `docs/config-reference.md`.

## Never commit

Passwords, API keys, tokens, real hostnames you want to keep private, or personal details. Secrets belong in `/srv/homelab/secrets/*.env` on the host (see `secrets.example/`). CI runs gitleaks on every push and pull request.

## Reporting a security problem

See [SECURITY.md](SECURITY.md). Please do not open a public issue for those.
