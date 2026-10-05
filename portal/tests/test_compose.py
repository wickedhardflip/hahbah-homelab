"""Guards on hosts/central/compose.yml that a unit test of the code alone can't catch."""
from pathlib import Path

import yaml

COMPOSE = yaml.safe_load((Path(__file__).parents[2] / "hosts/central/compose.yml").read_text(encoding="utf-8"))


def test_collector_mount_root_matches_where_mnt_is_mounted():
    col = COMPOSE["services"]["collector"]
    env = col["environment"]
    root = env["MOUNTS_ROOT"].rstrip("/")
    targets = {v.split(":")[0]: v.split(":")[1] for v in col["volumes"]}
    for path in env["MOUNTS"].split(","):   # e.g. /mnt/video must be visible at <root>/mnt/video
        top = "/" + path.strip("/").split("/")[0]
        assert targets.get(top) == root + top, f"{path}: {top} is mounted at {targets.get(top)}, but the collector looks in {root + top}"


def test_secrets_never_reach_the_web_facing_portal():
    portal = COMPOSE["services"]["portal"]
    files = [e if isinstance(e, str) else e["path"] for e in portal.get("env_file", [])]
    assert not any("porkbun" in f for f in files)
    assert not any("msmtprc" in v or "nas_collector" in v for v in portal.get("volumes", []))
