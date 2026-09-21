"""`exa agent version` / `exa agent alias` - agent versions as registry artifacts (ADR 0146).

A version is a content-addressed manifest pinning the prompt, model, tool and policy tuple by
digest or revision. ``alias set ... Production`` is gated on recorded evaluation evidence (and,
where signing is configured, a valid signature); ``alias rollback`` is one lookup. Nothing here
runs an agent: this is the registry, and ``resolve()`` is what a runtime reads.

Exit codes: 0 ok, 1 refused (invalid manifest, unknown version, policy or evidence gate).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from examlops import agent_versions as av
from examlops.cli import _output
from examlops.cli._policy_gate import enforce_and_confirm

_H = {"help_option_names": ["-h", "--help"]}

version_app = typer.Typer(
    help="Agent versions - immutable, content-addressed manifests (ADR 0146)",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings=_H,
)
alias_app = typer.Typer(
    help="Agent aliases - Staging / Canary / Production pointers, promotion gated by evidence",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings=_H,
)

_EX_REGISTER = (
    "Examples:\n\n  exa agent version register ./jobdoc.agent.yaml\n\n"
    "  exa -o json agent version register ./jobdoc.agent.json"
)
_EX_SHOW = "Examples:\n\n  exa agent version show av-sha256:3f9a...\n\n  exa agent version show jobdoc@Production"
_EX_LIST = "Examples:\n\n  exa agent version list\n\n  exa agent version list --agent jobdoc"
_EX_DIFF = "Examples:\n\n  exa agent version diff jobdoc@Staging jobdoc@Production"
_EX_SET = (
    "Examples:\n\n  exa agent alias set jobdoc Staging av-sha256:3f9a...\n\n"
    "  exa agent alias set jobdoc Production jobdoc@Staging --reason 'evals green'\n\n"
    "Production needs recorded evaluation evidence: see `exa eval gate set --help`."
)
_EX_SHOW_ALIAS = (
    "Examples:\n\n  exa agent alias show jobdoc\n\n  exa agent alias show jobdoc Production"
)
_EX_ROLLBACK = "Examples:\n\n  exa agent alias rollback jobdoc Production --reason 'regression'"


def _fail(code: str, error: str, **extra: Any) -> None:
    if _output.json_mode:
        _output.print_json({"ok": False, "code": code, "error": error, **extra})
        raise typer.Exit(1)
    _output.error(error, hint=code)


def _load(path: Path) -> Any:
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _fail("unreadable_manifest", f"cannot read {path}: {exc}")


@version_app.command("register", epilog=_EX_REGISTER)
def register_cmd(
    file: Path = typer.Argument(..., help="Manifest file (JSON or YAML)", exists=False),
) -> None:
    """Validate a manifest and register it; identical content returns the existing version."""
    doc = _load(file)
    try:
        out = av.register(doc)
    except av.AgentManifestError as exc:
        _fail("invalid_manifest", "invalid agent manifest", problems=exc.problems)
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    note = "registered" if out["created"] else "already registered (identical content)"
    _output.ok(f"{out['agent']} {out['version_id']} {note}{' [signed]' if out['signed'] else ''}")


def _record(row: dict[str, Any]) -> dict[str, Any]:
    m = row["manifest"]
    return {
        "version_id": row["version_id"],
        "agent": row["agent"],
        "signed": bool(row.get("signature")),
        "signature_valid": av.verify_signature(row) if row.get("signature") else None,
        "prompts": ", ".join(f"{p['name']}@{p['version']}" for p in m["prompts"]),
        "models": ", ".join(f"{x['role']}={x['servable']}" for x in m["models"]),
        "tools": len(m["tools"]["tools"]),
        "autonomy": m["policy"]["autonomy"],
        "image": m["code"]["image"],
    }


@version_app.command("show", epilog=_EX_SHOW)
def show_cmd(ref: str = typer.Argument(..., help="version id, or <agent>@<alias>")) -> None:
    """Show one version: the pinned tuple and whether its signature verifies."""
    row = av.get(ref)
    if row is None:
        _fail("not_found", f"unknown agent version {ref!r}")
        return
    if _output.json_mode:
        _output.print_json(
            {
                **{k: row[k] for k in ("version_id", "agent")},
                "manifest": row["manifest"],
                "signed": bool(row.get("signature")),
                "signature_valid": av.verify_signature(row) if row.get("signature") else None,
            }
        )
        return
    _output.print_record(_record(row))


@version_app.command("list", epilog=_EX_LIST)
def list_cmd(
    agent: str | None = typer.Option(None, "--agent", "-a", help="Only this agent"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500, help="Max versions"),
) -> None:
    """List registered versions, newest first, with the aliases pointing at each."""
    rows = av.list_versions(agent, limit=limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No agent versions registered")
        return
    _output.print_table(
        "Agent versions",
        ["version_id", "agent", "aliases", "signed"],
        [[r["version_id"], r["agent"], ",".join(r["aliases"]), str(r["signed"])] for r in rows],
    )


@version_app.command("diff", epilog=_EX_DIFF)
def diff_cmd(
    a: str = typer.Argument(..., help="version id or <agent>@<alias>"),
    b: str = typer.Argument(..., help="version id or <agent>@<alias>"),
) -> None:
    """Which components differ between two versions, by name."""
    try:
        out = av.diff(a, b)
    except LookupError as exc:
        _fail("not_found", str(exc))
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    if out["identical"]:
        _output.ok("Identical: no component differs")
        return
    _output.print_table(
        "Changed components",
        ["component", "change", "from", "to"],
        [[c["component"], c["change"], str(c["from"]), str(c["to"])] for c in out["changes"]],
    )


@alias_app.command("set", epilog=_EX_SET)
def set_cmd(
    agent: str = typer.Argument(..., help="Agent name"),
    alias: str = typer.Argument(..., help="Staging | Canary | Production"),
    ref: str = typer.Argument(..., help="version id, or <agent>@<alias> to copy an alias"),
    reason: str | None = typer.Option(None, "--reason", help="Why (recorded in the history)"),
) -> None:
    """Point an alias at a version. Production requires recorded evaluation evidence."""
    try:
        canon = av.canonical_alias(alias)
    except ValueError as exc:
        _fail("invalid_alias", str(exc))
        return
    ctx = {"agent": agent, "to_alias": canon, "alias": canon, "ref": ref}
    if not enforce_and_confirm(
        "agent_promote", ctx, what=f"moving {agent}@{canon}", prompt=f"Move {agent}@{canon}?"
    ):
        return
    try:
        out = av.set_alias(agent, canon, ref, reason=reason)
    except av.GateRefusal as exc:
        _fail("promotion_refused", f"promotion refused: {exc}", reasons=exc.reasons)
        return
    except LookupError as exc:
        _fail("not_found", str(exc))
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"{agent}@{canon} -> {out['version_id']} (was {out['previous'] or 'unset'})")


@alias_app.command("show", epilog=_EX_SHOW_ALIAS)
def show_alias_cmd(
    agent: str = typer.Argument(..., help="Agent name"),
    alias: str | None = typer.Argument(None, help="One alias; omit for all"),
) -> None:
    """Show where an agent's aliases point, and (for one alias) its recent moves."""
    from examlops.data import agent_versions as store

    rows = store.list_aliases(agent)
    if alias is not None:
        try:
            canon = av.canonical_alias(alias)
        except ValueError as exc:
            _fail("invalid_alias", str(exc))
            return
        rows = [r for r in rows if r["alias"] == canon]
    if not rows:
        _fail("not_found", f"{agent} has no {alias or 'alias'} set")
        return
    if _output.json_mode:
        hist = store.alias_history(agent, rows[0]["alias"]) if alias else None
        _output.print_json({"aliases": rows, "history": hist} if alias else rows)
        return
    _output.print_table(
        f"Aliases of {agent}",
        ["alias", "version_id", "actor"],
        [[r["alias"], r["version_id"], r["actor"] or ""] for r in rows],
    )


@alias_app.command("rollback", epilog=_EX_ROLLBACK)
def rollback_cmd(
    agent: str = typer.Argument(..., help="Agent name"),
    alias: str = typer.Argument(..., help="Staging | Canary | Production"),
    reason: str | None = typer.Option(None, "--reason", help="Why (recorded in the history)"),
) -> None:
    """Move an alias back to the version it held before its latest move (not re-gated)."""
    try:
        canon = av.canonical_alias(alias)
    except ValueError as exc:
        _fail("invalid_alias", str(exc))
        return
    ctx = {"agent": agent, "to_alias": canon, "alias": canon, "rollback": True}
    if not enforce_and_confirm(
        "agent_promote",
        ctx,
        what=f"rolling back {agent}@{canon}",
        prompt=f"Roll back {agent}@{canon}?",
    ):
        return
    try:
        out = av.rollback(agent, canon, reason=reason)
    except LookupError as exc:
        _fail("nothing_to_roll_back", str(exc))
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"{agent}@{canon} rolled back to {out['version_id']} (was {out['previous']})")
