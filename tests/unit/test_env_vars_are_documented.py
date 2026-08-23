"""Every environment variable the platform reads should be findable in one place.

`docs/reference/env-vars.md` is the published answer to "what can I set?". Nothing compared it to
the code, and it had fallen ~90 variables behind — including `PLATFORM_DB` (the datastore every
process opens), `EXAMLOPS_USECASE_DIR` (how the platform reaches its content at all),
`EXAMLOPS_DB_BACKEND` and the autopilot kill-switch. All four were documented in the repository's
private `CLAUDE.md` and in no public surface, which is the worst arrangement: the knowledge exists,
so nobody notices it is not published.

It also named `JUPYTERHUB_PORT`, which nothing reads — the Hub listens on 8000 and Compose maps
`18888:8000`.

This started as a **ratchet** over a 155-variable backlog. That backlog is now empty: every
variable the platform reads has a row, so the rule is absolute — a new variable is documented in the
change that introduces it. A second test fails if an exemption stops being true, which is what keeps
`UNDOCUMENTED` from quietly filling up again.

The scan is deliberately two-pronged. A literal `getenv("X")` is the obvious form, but the platform
also reads through helpers, and the getenv-only first version of this guard was blind to
`EXAMLOPS_BACKUP_TIERS` — the variable that decides what a backup contains.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "reference" / "env-vars.md"

# Two scans, because one is not enough. A literal `getenv("X")` is the obvious form; the platform
# also reads variables through helpers (`_get("EXAMLOPS_BACKUP_TIERS", …)`, `pick(…)`, `env.get(…)`,
# a `{"env": "MLFLOW_SQLITE_DB"}` spec row), and a getenv-only scan reports those as not existing.
# That is how `EXAMLOPS_BACKUP_TIERS` — the variable deciding what a backup contains — stayed
# invisible to the first version of this guard.
_READ = re.compile(r"""(?:getenv|environ\.get|env\.get)\(\s*["']([A-Z][A-Z0-9_]{2,})["']""")
_OURS = "EXAMLOPS|AGENT|DASHBOARD|CONTROL_PLANE|RAY|MLFLOW|PREFECT|SEANERBUS"
# `_[A-Z0-9]…[A-Z0-9]` and not a trailing underscore: the code also carries bare prefix strings
# (`"EXAMLOPS_"`, `"RAY_"`) used with startswith, and a looser pattern reads those as variables.
_LITERAL = re.compile(rf"""["']((?:{_OURS})_[A-Z0-9][A-Z0-9_]*[A-Z0-9])["']""")

# Set *by* the platform for a child process, not read from the operator's environment. Documented
# where the thing that receives them is documented, not as knobs.
INJECTED = {
    "EXAMLOPS_VLLM_MODEL",
    "EXAMLOPS_VLLM_ARGS",
    "EXAMLOPS_MODEL",
    "EXAMLOPS_PLATFORM_OPS",
    "EXAMLOPS_PLATFORM_SOURCE",
    "EXAMLOPS_ADMIN_SOURCE",
}

# Provided by the environment, not by ExaMLOps.
_NOT_OURS = {
    "PATH",
    "HOME",
    "USER",
    "CI",
    "PYTHONPATH",
    "TMPDIR",
    "LANG",
    "PWD",
    "SHELL",
    "TERM",
    "HOSTNAME",
    "VIRTUAL_ENV",
    "LOGNAME",
    "XDG_CONFIG_HOME",
    "NO_COLOR",
    "COLUMNS",
}

# Empty as of 2026-08-23: every variable the platform reads has a row. This is no longer a
# backlog but a hard rule — a new variable needs documentation in the same change. Re-adding an
# entry here is a deliberate decision to publish an undocumented knob, and needs a reason next to it.
UNDOCUMENTED: set[str] = set()


def _variables_read_in_code() -> set[str]:
    # `--others --exclude-standard` includes files that are new and not yet committed. Without it
    # the guard cannot fail on the change that introduces a variable — only on some later one, by
    # which point the undocumented knob is already released.
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    found: set[str] = set()
    for name in files:
        if name.startswith("tests/") or "/tests/" in name:
            continue
        text = (ROOT / name).read_text(errors="ignore")
        found |= set(_READ.findall(text)) | set(_LITERAL.findall(text))
    # CI_* is injected by GitLab; documenting GitLab's own contract is not this file's job.
    return {
        v for v in found if v not in _NOT_OURS and v not in INJECTED and not v.startswith("CI_")
    }


def _documented() -> set[str]:
    """Backticked names only — a bare uppercase scan counts prose (`NOTE`, `HTTP`) as coverage."""
    return set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", REFERENCE.read_text()))


def test_there_are_variables_to_check():
    """Otherwise every assertion below passes by inspecting nothing."""
    assert len(_variables_read_in_code()) >= 100, len(_variables_read_in_code())


def test_no_new_environment_variable_goes_undocumented():
    missing = sorted(_variables_read_in_code() - _documented() - UNDOCUMENTED)
    assert not missing, (
        "these environment variables are read in code and appear nowhere in "
        "docs/reference/env-vars.md:\n  " + "\n  ".join(missing) + "\nAdd a row for each — "
        "the backlog is empty, so this is a variable introduced without documentation."
    )


def test_the_backlog_does_not_outlive_its_reason():
    documented_now = sorted(UNDOCUMENTED & _documented())
    gone = sorted(UNDOCUMENTED - _variables_read_in_code() - _documented())
    assert not documented_now, (
        "these are documented now — remove them from UNDOCUMENTED: " + ", ".join(documented_now)
    )
    assert not gone, (
        "these are no longer read anywhere — remove them from UNDOCUMENTED: " + ", ".join(gone)
    )
