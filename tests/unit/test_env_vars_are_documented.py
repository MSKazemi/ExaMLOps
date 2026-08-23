"""Every environment variable the platform reads should be findable in one place.

`docs/reference/env-vars.md` is the published answer to "what can I set?". Nothing compared it to
the code, and it had fallen ~90 variables behind — including `PLATFORM_DB` (the datastore every
process opens), `EXAMLOPS_USECASE_DIR` (how the platform reaches its content at all),
`EXAMLOPS_DB_BACKEND` and the autopilot kill-switch. All four were documented in the repository's
private `CLAUDE.md` and in no public surface, which is the worst arrangement: the knowledge exists,
so nobody notices it is not published.

It also named `JUPYTERHUB_PORT`, which nothing reads — the Hub listens on 8000 and Compose maps
`18888:8000`.

This is a **ratchet**. `UNDOCUMENTED` freezes the backlog that existed when the guard was written,
so no *new* variable can be added without a row, and the list can only shrink. A second test fails
if an entry stops being true, so the exemption cannot outlive its reason.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "reference" / "env-vars.md"

_READ = re.compile(r"""(?:getenv|environ\.get)\(\s*["']([A-Z][A-Z0-9_]{2,})["']""")

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

# The backlog as measured on 2026-08-23. Shrink it; never add to it.
UNDOCUMENTED = {
    "AGENT_CLAUDE_MD",
    "AGENT_GRAPH_TIMEOUT",
    "AGENT_MEMORY_REVIEW_DB",
    "AGENT_SERVER_HOST",
    "AGENT_STREAM_IDLE_TIMEOUT",
    "APPROVAL_EXPIRY_HOURS",
    "CLIENT_SIM_DRIFT_COOLDOWN",
    "CLIENT_SIM_DRIFT_THRESHOLD",
    "CLIENT_SIM_DRIFT_WINDOW",
    "EXAMLOPS_ADMIN_SOURCE",
    "EXAMLOPS_BACKUP_BUCKETS",
    "EXAMLOPS_BACKUP_DIR",
    "EXAMLOPS_BACKUP_ON_PROMOTE",
    "EXAMLOPS_BACKUP_PG_DBS",
    "EXAMLOPS_BACKUP_S3_ACCESS_KEY",
    "EXAMLOPS_BACKUP_S3_ENDPOINT",
    "EXAMLOPS_BACKUP_S3_SECRET",
    "EXAMLOPS_BACKUP_S3_URI",
    "EXAMLOPS_CONFIG_DIR",
    "EXAMLOPS_FAIRNESS_GATE_ENABLED",
    "EXAMLOPS_GRID_INTENSITY_TOKEN",
    "EXAMLOPS_GRID_INTENSITY_URL",
    "EXAMLOPS_GRID_INTENSITY_ZONE",
    "EXAMLOPS_KSERVE_GATEWAY_URL",
    "EXAMLOPS_KSERVE_LIVE_APPLY",
    "EXAMLOPS_LLM_COST_PROVIDER",
    "EXAMLOPS_LLM_LAUNCHER",
    "EXAMLOPS_POLICY_BUNDLE_DIR",
    "EXAMLOPS_POLICY_ENGINE",
    "EXAMLOPS_REPO_ROOT",
    "EXAMLOPS_SYNTHETIC_ONLY_GATE",
    "EXAMLOPS_TENANT",
    "EXAMLOPS_VAULT_TOKEN",
    "EXAMLOPS_VLLM_API_KEY",
    "EXAMLOPS_VLLM_BASE_URL",
    "EXAMLOPS_VLLM_CONNECT_TIMEOUT",
    "EXAMLOPS_VLLM_HOST_PORT",
    "EXAMLOPS_VLLM_IMAGE",
    "EXAMLOPS_VLLM_MODULES",
    "EXAMLOPS_VLLM_PORT",
    "EXAMLOPS_VLLM_RAY_PORT",
    "EXAMLOPS_VLLM_TIMEOUT",
    "EXAMLOPS_VLLM_WORK_DIR",
    "FEATURE_STORE_DIR",
    "IDEMPOTENCY_TTL_SECONDS",
    "LOG_FORMAT",
    "MINIO_ROOT_PASSWORD",
    "MINIO_ROOT_USER",
    "MODELS",
    "NOTIFICATION_WEBHOOK_URL",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "PGHOST",
    "PGPASSWORD",
    "PGPORT",
    "PGUSER",
    "POSTGRES_PASSWORD",
    "POSTGRES_USER",
    "PREFECT_CB_FAIL_MAX",
    "PREFECT_CB_RESET_TIMEOUT",
    "RAY_MLFLOW_BACKOFF_FACTOR",
    "RAY_WORKER_ID",
    "RETRAIN_DATASET",
    "RETRAIN_DUMMY",
    "RETRAIN_RATE_LIMIT_PER_MIN",
    "SEANERBUS_PUBLISH_RESULTS",
}


def _variables_read_in_code() -> set[str]:
    files = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    found: set[str] = set()
    for name in files:
        if name.startswith("tests/") or "/tests/" in name:
            continue
        found |= set(_READ.findall((ROOT / name).read_text(errors="ignore")))
    # CI_* is injected by GitLab; documenting GitLab's own contract is not this file's job.
    return {v for v in found if v not in _NOT_OURS and not v.startswith("CI_")}


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
        "docs/reference/env-vars.md:\n  " + "\n  ".join(missing) + "\nAdd a row for each. "
        "(UNDOCUMENTED is a frozen backlog — it may shrink, never grow.)"
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
