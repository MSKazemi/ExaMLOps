"""The guard that FORCES a new dashboard mutation to consult policy (ADR 0079 d2).

Every POST/PUT/PATCH/DELETE operation the app exposes must be classified in
``policy_gate.ROUTE_POLICY`` as gated (with an action) or exempt (with a reason). Adding a mutating
route without deciding which fails here — that is the whole mechanism.

Source of truth is ``app.openapi()['paths']`` — public API with one meaning on every FastAPI
release (0.141 no longer flattens ``include_router`` into ``app.routes``, so a walk of
``app.routes`` finds nothing there). Its limit: an operation registered with
``include_in_schema=False`` is not in the schema; ``walk_mutating`` adds whatever a recursive walk
of ``app.routes`` can still see, best-effort, so such a route is caught where the walk works.
"""

import inspect
from pathlib import Path

from main import app
from policy_gate import (
    ROUTE_POLICY,
    Exempt,
    Gate,
    normalize,
    openapi_mutating,
    resolve,
    walk_mutating,
)

# Actions the CLI already gates (examlops.cli._policy_gate callers) — reused, not re-invented.
_CLI_VOCABULARY = {
    "manual_promote",
    "cluster_approve",
    "cluster_reject",
    "project_delete",
    "project_remove_member",
}
# Reviewed exceptions to the `dashboard_<router>_<verb>` naming rule.
_OTHER_NAMES = {"approval_approve", "approval_reject"}


def _table() -> set[tuple[str, str]]:
    return {(m, normalize(p)) for m, p in ROUTE_POLICY}


def _live() -> set[tuple[str, str]]:
    schema = openapi_mutating(app)
    assert schema, "openapi() lists no mutating operation — the guard would pass vacuously"
    return schema | walk_mutating(app.routes)


def test_every_mutating_route_is_classified():
    unclassified = sorted(_live() - _table())
    assert not unclassified, (
        "these mutating dashboard routes neither consult policy nor say why not — add each to "
        "policy_gate.ROUTE_POLICY as Gate(<action>) or Exempt(<reason>):\n  "
        + "\n  ".join(f"{m} {p}" for m, p in unclassified)
    )


def test_the_table_names_no_route_that_does_not_exist():
    stale = sorted(_table() - _live())
    assert not stale, f"stale ROUTE_POLICY entries (route removed or renamed): {stale}"


def test_every_exemption_states_a_real_reason():
    for key, entry in ROUTE_POLICY.items():
        if isinstance(entry, Exempt):
            assert len(entry.reason.split()) >= 5, f"{key}: an exemption needs a reason"


def test_action_names_follow_the_vocabulary():
    for key, entry in ROUTE_POLICY.items():
        if isinstance(entry, Gate):
            a = entry.action
            assert a in _CLI_VOCABULARY or a in _OTHER_NAMES or a.startswith("dashboard_"), (
                f"{key}: action {a!r} is neither a CLI action nor dashboard_<router>_<verb>"
            )


def test_the_gate_is_an_app_level_dependency_not_a_mutated_route():
    """Public-API wiring: `FastAPI(dependencies=[...])`, so it works the same on every release."""
    from policy_gate import policy_gate

    assert any(d.dependency is policy_gate for d in app.router.dependencies)


def test_every_template_resolves_to_itself():
    """A concrete request must land on the entry written for its route, not a neighbour's.

    `/projects/onboard/{model}` and `/projects/{name}/resources` overlap for a model named
    `resources`; the most specific template has to win, as Starlette's routing makes it.
    """
    import re

    for method, template in ROUTE_POLICY:
        concrete = re.sub(r"\{\w+(?::\w+)?\}", "x", template)
        hit = resolve(method, concrete)
        assert hit is not None and hit[0] == template, (method, template, hit)


def test_platform_ops_exemption_is_true():
    """The three platform-ops writes are exempt only because PlatformAdmin gates them."""
    src = Path(
        inspect.getsourcefile(__import__("routers.platform_ops", fromlist=["x"]))
    ).read_text()
    assert "platform_admin" in src and "PlatformAdmin" in src


def test_cli_console_exemption_is_true():
    """`/cli/runs` is exempt because the CLI subprocess enforces policy: it must be `exa`."""
    src = Path(inspect.getsourcefile(__import__("cli_runner"))).read_text()
    assert "examlops.cli" in src
