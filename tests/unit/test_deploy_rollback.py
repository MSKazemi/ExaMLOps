"""The rollback path must not destroy what the deploy path was fixed to preserve.

`deploy:lxp` deliberately does **not** pass `--remove-orphans`: on this compose file that
flag deletes every profile service (monitoring ×6, jupyter, vllm, the SeanerBUS bridge),
which is what commit a9035877 — "persist on-demand services across deploys" — was written to
stop. The rollback in `smoke:lxp` kept its copy of the flag, so the recovery path did exactly
the thing the deploy path forbids, at the one moment production is already broken and nobody
would connect the missing Grafana/JupyterHub to a rollback.

These are cheap structural assertions, not a pipeline run — but the divergence they catch is
the kind that only ever shows up in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

CI = Path(__file__).parents[2] / ".gitlab-ci.yml"
RELEASE_SCRIPT = Path(__file__).parents[2] / "platform" / "ci" / "lxp_release.sh"


def _script(job: str) -> str:
    d = yaml.safe_load(CI.read_text())
    assert job in d, f"{job} is not in .gitlab-ci.yml"
    parts = []
    for section in ("before_script", "script", "after_script"):
        for line in d[job].get(section) or []:
            parts.append(line if isinstance(line, str) else str(line))
    # Drop whole-line shell comments. deploy:lxp carries a NOTE that quotes the very flag
    # these tests forbid ("NOTE: NO --remove-orphans — it deletes the on-demand profile
    # services"), and matching the warning as if it were the offence is how a guard test
    # cries wolf. Only what the shell would execute counts.
    return "\n".join(ln for ln in "\n".join(parts).splitlines() if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("job", ["deploy:lxp", "smoke:lxp"])
def test_no_remove_orphans_on_the_lxp_stack(job: str):
    assert "--remove-orphans" not in _script(job), (
        f"`{job}` passes --remove-orphans to docker compose. On this compose file that deletes "
        "every profile service — the six monitoring containers, JupyterHub, vllm and the "
        "SeanerBUS bridge — so Grafana embeds, project workbenches and the bus tab break. "
        "deploy:lxp was fixed for exactly this in a9035877; keep both paths identical."
    )


def test_rollback_refuses_an_empty_previous_release():
    """An absent/empty artifact must abort rather than activating an empty path."""
    script = _script("smoke:lxp")
    assert "PREV_RELEASE" in script, "smoke:lxp no longer records the previous release"
    guard = '-z "$PREV_RELEASE"'
    assert guard in script, (
        "smoke:lxp must refuse an empty previous-release artifact instead of passing an empty "
        "path to the remote activation script"
    )


def test_deploy_does_not_mutate_the_legacy_checkout():
    script = _script("deploy:lxp")
    assert "git pull" not in script
    assert "git reset" not in script
    assert "git archive" in script
    assert "lxp_release.sh" in script


def test_first_release_falls_back_to_the_legacy_checkout():
    script = _script("deploy:lxp")
    assert "if [ -L" in script
    assert "${LXP_DEPLOY_PATH}-current" in script
    assert "readlink -f" in script
    assert "printf '%s\\\\n'" in script


def test_rollback_reactivates_a_release_instead_of_resetting_git():
    script = _script("smoke:lxp")
    assert "git reset" not in script
    assert "lxp_release.sh" in script
    assert " activate " in script


def test_rollback_still_fails_the_pipeline():
    """A rollback is a recovery, not a success: the bad commit must still be signalled."""
    script = _script("smoke:lxp")
    assert script.rstrip().splitlines()[-1].startswith("exit 1"), (
        "smoke:lxp must end with `exit 1` so a rolled-back deploy still fails the pipeline"
    )


def test_release_script_is_valid_under_bash_strict_mode():
    """Assignments must not expand a local before Bash has initialized it."""
    text = RELEASE_SCRIPT.read_text()
    assert 'local sha="$1" archive="$2" release_path=' not in text
    assert 'local sha="$1" archive="$2"' in text
    assert 'local release_path="$RELEASE_ROOT/$sha"' in text
