"""Core ⟂ deployment: runtime code must not learn where, or how, it is deployed (ADR 0129 §8).

A published wheel or image has no checkout around it. Code that reaches up the directory tree for
the repository root, puts repository paths on ``sys.path``, or hard-codes the deployment's layout
(the compose bind mount at ``/repo``, ``platform/infra/``, a compose file, the CI config) works on
the one machine that has the checkout and nowhere else — and it welds the product to one way of
running it. ADR 0128 moves instance data behind ``EXAMLOPS_DATA_DIR``; this guard keeps the
*code* side of the split from growing back.

It is a **ratchet**, measured per file on 2026-09-10 (HEAD plus the slices in flight that day).
A file may not gain a coupling it did not have, and a new file may not introduce one. When a count
falls, lower its entry (or delete it) in the same change — ``test_baseline_is_tight`` fails until
you do, so a removed coupling cannot quietly come back. Three signatures:

* ``sys_path`` — ``sys.path.insert/append/extend``: the package boundary is not real.
* ``repo_root`` — ``.parents[N>=2]`` / ``.parent.parent.parent``: reaching out of the package.
* ``deploy_ref`` — a string naming deployment topology: ``/repo``, ``platform/infra/``,
  ``docker-compose``, ``.gitlab-ci``.

Runtime code only: tests, conftest files and node_modules are out of scope. The largest cluster —
45 dashboard routers defaulting ``PLATFORM_DB`` to ``/repo/platform.db`` — goes away by routing
them through ``settings.platform_db``.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit._guard_deps import scan_files

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOTS = (
    "platform/cli/src/examlops",
    "pipelines",
    "serving",
    "platform/services",
    "platform/clients",
)
SIGNATURES = {
    "sys_path": re.compile(r"\bsys\.path\.(?:insert|append|extend)\("),
    "repo_root": re.compile(r"\.parents\[\s*[2-9]\s*\]|\.parent\.parent\.parent"),
    "deploy_ref": re.compile(
        r"""["'][^"'\n]*(?:platform/infra/|docker-compose|\.gitlab-ci|(?<![\w.])/repo(?:/|["']))"""
    ),
}

# Per-file ceilings. Only ever lower these; a new entry needs a reason in review.
BASELINE: dict[str, dict[str, int]] = {
    "pipelines/deploy.py": {"sys_path": 1},
    "pipelines/pipeline_generator.py": {"sys_path": 1},
    "pipelines/slurm_train_script.py": {"sys_path": 1},
    "pipelines/usecase.py": {"sys_path": 1},
    "platform/cli/src/examlops/cli/commands/agent_cmd.py": {"repo_root": 1},
    "platform/cli/src/examlops/cli/commands/data_cmd.py": {"repo_root": 2, "sys_path": 2},
    "platform/cli/src/examlops/cli/commands/hpc_cmd.py": {
        "deploy_ref": 1,
        "repo_root": 1,
        "sys_path": 1,
    },
    "platform/cli/src/examlops/cli/commands/project_cmd.py": {"deploy_ref": 1},
    "platform/cli/src/examlops/cli/commands/stack.py": {"deploy_ref": 1},
    "platform/cli/src/examlops/cli/commands/synth_cmd.py": {"repo_root": 1, "sys_path": 1},
    "platform/cli/src/examlops/llm_endpoints.py": {"deploy_ref": 2, "repo_root": 3, "sys_path": 1},
    "platform/cli/src/examlops/platform_db.py": {"repo_root": 1},
    "platform/cli/src/examlops/scheduler_jobs.py": {"repo_root": 1, "sys_path": 1},
    "platform/cli/src/examlops/workbench_spawn.py": {"deploy_ref": 1},
    "platform/clients/model_schema_registry.py": {"repo_root": 1},
    "platform/clients/seanerbus_bridge.py": {"sys_path": 2},
    "platform/clients/seanerbus_test_pub.py": {"sys_path": 1},
    "platform/clients/seanerbus_test_req.py": {"repo_root": 1, "sys_path": 1},
    "platform/services/agent/agent.py": {"sys_path": 1},
    "platform/services/agent/agent_server.py": {"sys_path": 1},
    "platform/services/agent/skipper/config.py": {"repo_root": 1},
    "platform/services/agent/skipper/tools/finops.py": {"repo_root": 1, "sys_path": 1},
    "platform/services/agent/skipper/tools/platform_ops.py": {"repo_root": 1, "sys_path": 1},
    "platform/services/control_plane/api_contract.py": {"sys_path": 1},
    "platform/services/control_plane/model_meta.py": {"repo_root": 1},
    "platform/services/dashboard/backend/alembic/env.py": {"sys_path": 1},
    "platform/services/dashboard/backend/cli_runner.py": {"repo_root": 1},
    # The dashboard's ONE legacy compose default (was 45 copies, one per module).
    "platform/services/dashboard/backend/dbconn.py": {"deploy_ref": 1},
    "platform/services/dashboard/backend/routers/config.py": {"deploy_ref": 2},
    "platform/services/dashboard/backend/routers/scaffold.py": {"deploy_ref": 1},
    "platform/services/dashboard/backend/settings.py": {"deploy_ref": 1},
    "serving/ray_serving/app.py": {"repo_root": 1},
}


def _is_runtime(path: Path) -> bool:
    parts = path.relative_to(ROOT).parts
    if any(p in ("tests", "node_modules", "__pycache__") for p in parts):
        return False
    return not path.name.startswith("test_") and path.name != "conftest.py"


def _measure() -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for root in RUNTIME_ROOTS:
        for path in scan_files(ROOT / root):
            if not _is_runtime(path):
                continue
            text = path.read_text(errors="replace")
            counts = {kind: len(rx.findall(text)) for kind, rx in SIGNATURES.items()}
            counts = {kind: n for kind, n in counts.items() if n}
            if counts:
                found[path.relative_to(ROOT).as_posix()] = counts
    return found


def test_no_runtime_file_gains_a_deployment_coupling():
    grown = []
    for path, counts in _measure().items():
        ceiling = BASELINE.get(path, {})
        for kind, n in counts.items():
            if n > ceiling.get(kind, 0):
                grown.append(f"{path}: {kind} {ceiling.get(kind, 0)} -> {n}")
    assert not grown, (
        "runtime code gained a coupling to the checkout or the deployment layout:\n  "
        + "\n  ".join(grown)
        + "\nResolve paths through the package (importlib.resources), configuration (settings /"
        " env), or the ADR 0128 data root — never through the repository."
    )


def test_baseline_is_tight():
    """A coupling that was removed must leave the baseline too, or it can come back unseen."""
    measured = _measure()
    loose = []
    for path, ceiling in BASELINE.items():
        if not (ROOT / path).exists():
            # Measured from a slice still in flight on 2026-09-10; see the plan's follow-up to
            # make a missing file fail once those slices have landed.
            continue
        for kind, allowed in ceiling.items():
            now = measured.get(path, {}).get(kind, 0)
            if now < allowed:
                loose.append(f"{path}: {kind} {allowed} -> {now}")
    assert not loose, "lower these BASELINE entries (the coupling is gone):\n  " + "\n  ".join(
        loose
    )


def test_the_signatures_match_what_they_claim():
    """The guard is only as good as its patterns — pin what each one catches and ignores."""
    assert SIGNATURES["sys_path"].search("sys.path.insert(0, str(root))")
    assert not SIGNATURES["sys_path"].search("sys.path_hooks")
    assert SIGNATURES["repo_root"].search("Path(__file__).resolve().parents[4]")
    assert not SIGNATURES["repo_root"].search("Path(__file__).parents[1]")
    assert SIGNATURES["deploy_ref"].search('os.getenv("PLATFORM_DB", "/repo/platform.db")')
    assert SIGNATURES["deploy_ref"].search('"platform/infra/docker-compose/docker-compose.yml"')
    assert not SIGNATURES["deploy_ref"].search('"/api/repos/list"')
    assert not SIGNATURES["deploy_ref"].search('"my/repo/x"')
