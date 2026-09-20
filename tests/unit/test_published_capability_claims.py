"""Guard the two capability claims the platform publishes to *machines*.

A claim a machine reads and acts on is worth more scrutiny than a docstring, because nothing
downstream re-checks it:

* **MCP** — ``ToolSpec.mutating`` decides whether a tool is exposed at all when the server runs
  read-only (``EXAMLOPS_MCP_ALLOW_WRITES`` unset), and ``tier`` tells an autonomous caller how
  much confirmation an action needs. A mutating function registered as read-only is a privilege
  escalation, not a documentation slip.
* **Dashboard** — the ``/me`` capability list drives which affordances the UI renders, but the
  BFF is the sole enforcement point (F15). An unguarded mutating route is a control the UI
  believes is admin-only and the server lets anyone call.

Both checks fail in *both* directions: an unguarded route is a failure, and so is leaving a route
on the deliberately-viewer-writable allow-list after it grows a guard.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from examlops.mcp.tools import REGISTRY

REPO = Path(__file__).resolve().parents[2]

# Call shapes that mutate state. Matched against a tool's own body, which is where the call to
# any writing helper appears regardless of which module the helper lives in.
_WRITE_CALL = re.compile(
    r"\b((?:set|write|delete|create|insert|update|record|claim|grant|approve|reject|enable"
    r"|disable|trigger|register|bind|save|prune|rewrap|promote)_\w+)\s*\("
)
_WRITE_VERB = re.compile(
    r"\.(post|put|patch|delete)\s*\(|execute\s*\(\s*[\"'](?:INSERT|UPDATE|DELETE)"
)


@pytest.mark.parametrize("spec", [s for s in REGISTRY if not s.mutating], ids=lambda s: s.name)
def test_read_tools_do_not_mutate(spec) -> None:
    """A tool advertised as read-only must not reach a write path."""
    src = inspect.getsource(spec.fn)
    calls = sorted({m.group(1) for m in _WRITE_CALL.finditer(src)})
    assert not calls, (
        f"{spec.name} is registered with mutating=False but calls {calls}. A read-only MCP "
        f"server exposes it, so this is a privilege escalation — set mutating=True and a tier."
    )
    assert not _WRITE_VERB.search(src), (
        f"{spec.name} is registered with mutating=False but issues an HTTP/SQL write."
    )


@pytest.mark.parametrize("spec", [s for s in REGISTRY if s.mutating], ids=lambda s: s.name)
def test_mutating_tools_are_gated(spec) -> None:
    """Every mutating tool must consult the least-privilege write gate (ADR 0102)."""
    if spec.name in {"plan_change", "apply_plan", "approve_plan"}:
        # They delegate: plan_change/apply_plan run the *target* tool's own gate (probe / apply),
        # asserted for every plannable tool in tests/unit/test_plan_apply.py.
        return
    src = inspect.getsource(spec.fn)
    assert "_agent_write_gate" in src, (
        f"{spec.name} is registered as mutating but never calls _agent_write_gate."
    )


def test_tier_and_mutating_agree() -> None:
    """``tier == 'read'`` and ``mutating is False`` are the same statement; keep them in step."""
    mismatched = [
        (s.name, s.mutating, s.tier) for s in REGISTRY if s.mutating == (s.tier == "read")
    ]
    assert not mismatched, f"tier/mutating disagree: {mismatched}"


# ── dashboard: every mutating route is guarded ────────────────────────────────

_ROUTERS = REPO / "platform" / "services" / "dashboard" / "backend" / "routers"
_MUTATING_VERBS = {"post", "put", "patch", "delete"}

#: Routes a *viewer* is deliberately allowed to call. Each is a write that belongs to the
#: viewer's own session or is read-shaped despite being a POST. Anything not listed must be
#: admin- or capability-guarded.
_VIEWER_WRITABLE = {
    ("auth.py", "login"),  # pre-auth by definition
    ("auth.py", "logout"),
    ("sso.py", "logout"),  # ADR 0120: clears the caller's own SSO session cookie — nothing else
    ("alerts.py", "ack"),  # operational acknowledgement, recorded with the actor
    ("collab.py", "add_comment"),  # F22 collaboration is a viewer affordance
    ("collab.py", "create_snapshot"),
    ("copilot.py", "ask"),  # F11 copilot is propose-only
    ("models.py", "predict"),  # inference: a POST because it carries a body, not a write
    ("selfobs.py", "ui_action"),  # F24 telemetry of the caller's own UI action
    # ADR 0119: stopping a CLI run you started. A viewer can only see — so only cancel — its own
    # runs (`_visible`), and those are `read`-tier by construction; admins may cancel any.
    ("cli.py", "cancel_run"),
}


# Mutating routes authenticated by a credential OTHER than a dashboard session, so neither a
# capability nor a role applies. Each is guarded by its own check; the reason says which.
_OTHER_CREDENTIAL = {
    # ADR 0132 SCIM: the center's IdP authenticates with its own per-provider SCIM bearer
    # (`examlops.iam.scim.authenticate`, provisioning.token_ref) and is confined to its own accounts.
    ("scim.py", "create_user"),
    ("scim.py", "replace_user"),
    ("scim.py", "patch_user"),
    ("scim.py", "delete_user"),
}


def _guard_helpers(tree: ast.Module, src: str) -> set[str]:
    """Module-level functions that themselves perform a capability/role check."""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            seg = ast.get_source_segment(src, node) or ""
            if "deny_reason" in seg or "require_capability" in seg or "HTTP_403" in seg:
                names.add(node.name)
    return names


def _mutating_routes() -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    for path in sorted(_ROUTERS.glob("*.py")):
        src = path.read_text()
        tree = ast.parse(src)
        helpers = _guard_helpers(tree, src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            verbs = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in _MUTATING_VERBS
            ]
            if not verbs:
                continue
            seg = ast.get_source_segment(src, node) or ""
            guarded = (
                "require_capability" in seg
                or "Depends(_admin" in seg  # module-level `_admin = require_role("admin")`
                or 'require_role("admin")' in seg
                or any(f"{h}(" in seg for h in helpers)
            )
            out.append((path.name, node.name, guarded))
    return out


@pytest.mark.skipif(not _ROUTERS.is_dir(), reason="dashboard backend not present")
def test_every_mutating_dashboard_route_is_guarded() -> None:
    routes = _mutating_routes()
    assert routes, "found no mutating routes — the scan itself is broken"
    unguarded = {(f, n) for f, n, ok in routes if not ok}
    surprises = sorted(unguarded - _VIEWER_WRITABLE - _OTHER_CREDENTIAL)
    assert not surprises, (
        "mutating dashboard routes with no capability or admin guard: "
        f"{surprises}. Guard them, or add them to _VIEWER_WRITABLE with the reason."
    )


@pytest.mark.skipif(not _ROUTERS.is_dir(), reason="dashboard backend not present")
def test_viewer_writable_allowlist_has_no_stale_entries() -> None:
    """The other direction: a route that grew a guard must leave the allow-list."""
    routes = _mutating_routes()
    unguarded = {(f, n) for f, n, ok in routes if not ok}
    known = {(f, n) for f, n, _ in routes}
    listed = _VIEWER_WRITABLE | _OTHER_CREDENTIAL
    stale = sorted(e for e in listed if e in known and e not in unguarded)
    assert not stale, f"these are guarded now and should leave the allow-lists: {stale}"
    gone = sorted(e for e in listed if e not in known)
    assert not gone, f"_VIEWER_WRITABLE names routes that no longer exist: {gone}"
