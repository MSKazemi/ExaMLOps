"""Every control-plane capability is reachable by an operator — from `exa` or the dashboard.

The control plane is the other operator surface next to the CLI. A route only it exposes is a
capability an operator cannot use without hand-crafting HTTP calls — `GET /retrain/{flow_run_id}`
(is my retrain done?) and `POST /admin/reload` (pick up new model YAML without a restart) were
exactly that. Anything the CLI calls is in the dashboard too (CLI Console, ADR 0119).

The route list comes from the committed API contract (`api-contract.json`), so a new route fails
this test until it is wired to a command or exempted here with a reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "platform" / "services" / "control_plane" / "api-contract.json"
# Operator surfaces: `exa` commands and the dashboard. (The MCP server is an *agent* surface — a
# route only an MCP tool calls is still out of an operator's reach.)
SURFACES = [
    REPO / "platform" / "cli" / "src" / "examlops" / "cli",
    REPO / "platform" / "services" / "dashboard" / "backend",
    REPO / "platform" / "services" / "dashboard" / "frontend" / "src",
]

# Routes that are not operator actions, and why.
NOT_OPERATOR_ROUTES = {
    "/webhooks/modelzoo/github": "Inbound push webhook called by GitHub, not by an operator.",
    "/webhooks/modelzoo/gitlab": "Inbound push webhook called by GitLab, not by an operator.",
    "/api/changes": "CI change notification (platform/ci/notify_model_changes.py) that opens "
    "approvals; operators act on the result through `exa approvals`.",
}


def _static_prefix(route: str) -> str:
    """`/retrain/{flow_run_id}` → `/retrain/`; `/models/{name}/meta` → `/meta` is checked too."""
    return route.split("{", 1)[0]


def _source_text() -> str:
    parts = []
    for root in SURFACES:
        for path in root.rglob("*"):
            if (
                path.suffix in {".py", ".ts", ".tsx"}
                and "node_modules" not in path.parts
                and "test" not in path.name
            ):
                parts.append(path.read_text(errors="ignore"))
    return "\n".join(parts)


def test_every_control_plane_route_is_reachable_by_an_operator():
    routes = sorted(json.loads(CONTRACT.read_text())["paths"])
    text = _source_text()
    unreachable = []
    for route in routes:
        if route in NOT_OPERATOR_ROUTES:
            continue
        prefix = _static_prefix(route)
        tail = route.rsplit("}", 1)[-1] if "}" in route else ""
        # A URL being built — the route right after a quote or an interpolation — not prose that
        # happens to contain the words ("deploy/retrain/reload" in a help string).
        built = re.search(r"""["'`}]""" + re.escape(prefix), text)
        if not built or (tail and tail not in text):
            unreachable.append(route)
    assert not unreachable, (
        f"control-plane routes no CLI command or dashboard route uses: {unreachable}. Add an "
        "`exa` command (the dashboard gets it through the CLI Console) or exempt the route in "
        "NOT_OPERATOR_ROUTES with the reason."
    )


def test_exemptions_name_real_routes():
    routes = set(json.loads(CONTRACT.read_text())["paths"])
    assert set(NOT_OPERATOR_ROUTES) <= routes
