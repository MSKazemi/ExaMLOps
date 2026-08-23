"""A compose file must stay usable on a machine that does not run every service.

Compose interpolates **every** service before it filters by profile. So a required-variable
expression (``${VAR:?message}``) inside a profile-gated service does not guard that service —
it aborts *every* compose command, including ``ps``, ``logs`` and ``down``, on any machine
that has not set the variable. That is precisely the machine the profile exists to protect.

Measured on this laptop before the fix::

    $ docker compose -f platform/infra/docker-compose/docker-compose.yml ps
    error while interpolating services.vllm.command: required variable EXAMLOPS_VLLM_MODEL
    is missing a value

The stack could not be inspected, started or stopped because of a GPU service nobody had
asked for. Four `:?` expressions remain, all on the dashboard — a default-profile service
that genuinely cannot run without its secrets, which is what `:?` is for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = sorted((ROOT / "platform" / "infra").rglob("docker-compose*.y*ml"))

_REQUIRED = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):\?")


def test_there_are_compose_files_to_check():
    """Otherwise every assertion below passes by inspecting nothing."""
    assert len(COMPOSE) >= 2, [str(p) for p in COMPOSE]


@pytest.mark.parametrize("path", COMPOSE, ids=lambda p: p.name)
def test_no_profile_gated_service_declares_a_required_variable(path: Path):
    doc = yaml.safe_load(path.read_text()) or {}
    offenders: list[str] = []
    for name, service in (doc.get("services") or {}).items():
        if not isinstance(service, dict) or not service.get("profiles"):
            continue
        for var in _REQUIRED.findall(yaml.safe_dump(service)):
            offenders.append(f"{path.name}: service '{name}' (profile-gated) requires ${var}")
    assert not offenders, (
        "compose interpolates before filtering by profile, so these abort every compose "
        "command on a machine that does not set the variable — use a default instead:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("path", COMPOSE, ids=lambda p: p.name)
def test_every_compose_file_parses(path: Path):
    doc = yaml.safe_load(path.read_text())
    assert isinstance(doc, dict) and ("services" in doc or "include" in doc), path
