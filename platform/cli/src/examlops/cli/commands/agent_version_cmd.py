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


_EX_CARD = (
    "Examples:\n\n  exa agent version card jobdoc\n\n  exa agent version card jobdoc@Staging"
    " --out card.json"
)


@version_app.command("card", epilog=_EX_CARD)
def card_cmd(
    ref: str = typer.Argument(..., help="version id, <agent>@<alias>, or <agent> (Production)"),
    out: Path | None = typer.Option(None, "--out", help="Write the card JSON to this file"),
) -> None:
    """Print the A2A-shaped Agent Card of a registered version (read-only, content-addressed)."""
    import json as _json

    from examlops.mcp.agent_card import build_agent_version_card, card_digest

    row = av.get(ref if ("@" in ref or ref.startswith("av-")) else f"{ref}@Production")
    if row is None:
        _fail("not_found", f"unknown agent version {ref!r}")
        return
    card = build_agent_version_card(
        av.AgentVersion(
            version_id=row["version_id"],
            agent=row["agent"],
            manifest=row["manifest"],
            signed=bool(row.get("signature")),
        )
    )
    if out is not None:
        try:
            out.write_text(_json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            _fail("write_failed", f"cannot write {out}: {exc}")
            return
        if not _output.json_mode:
            _output.ok(f"wrote {out} ({card_digest(card)})")
            return
    if _output.json_mode:
        _output.print_json(card)
        return
    if out is None:
        typer.echo(_json.dumps(card, indent=2, sort_keys=True))


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
    state_strategy: str | None = typer.Option(
        None,
        "--state-strategy",
        help="pin | drain: required to replace Production with an incompatible or unknown "
        "checkpoint schema (ADR 0146 d5)",
    ),
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
        out = av.set_alias(agent, canon, ref, reason=reason, state_strategy=state_strategy)
    except ValueError as exc:
        _fail("invalid_argument", str(exc))
        return
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
    in_flight: str = typer.Option(
        "continue",
        "--in-flight",
        help="continue | interrupt | quarantine: what the agent runtime does with sessions "
        "still running on the rolled-back version (ADR 0146 d4)",
    ),
) -> None:
    """Move an alias back to the version it held before its latest move (not re-gated)."""
    if in_flight not in av.IN_FLIGHT_POLICIES:
        _fail("invalid_argument", f"--in-flight must be one of {', '.join(av.IN_FLIGHT_POLICIES)}")
        return
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
        out = av.rollback(agent, canon, reason=reason, in_flight=in_flight)
    except LookupError as exc:
        _fail("nothing_to_roll_back", str(exc))
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"{agent}@{canon} rolled back to {out['version_id']} (was {out['previous']})")


# -- ADR 0146 d4: session canary -----------------------------------------------------------------

_EX_CANARY = (
    "Examples:\n\n  exa agent alias canary jobdoc 10 --reason 'try v8 on new sessions'\n\n"
    "  exa agent alias canary jobdoc 0      # stop sending new sessions to Canary"
)


@alias_app.command("canary", epilog=_EX_CANARY)
def canary_cmd(
    agent: str = typer.Argument(..., help="Agent name"),
    percent: float = typer.Argument(..., help="Share of NEW sessions started on Canary (0-100)"),
    reason: str | None = typer.Option(None, "--reason", help="Why (recorded in the audit log)"),
) -> None:
    """Start a share of the agent's new sessions on its Canary version; existing ones stay put."""
    ctx = {"agent": agent, "to_alias": "Canary", "alias": "Canary", "canary_percent": percent}
    if not enforce_and_confirm(
        "agent_promote",
        ctx,
        what=f"routing {percent}% of new {agent} sessions to Canary",
        prompt=f"Send {percent}% of new {agent} sessions to Canary?",
    ):
        return
    try:
        out = av.set_canary(agent, percent, reason=reason)
    except ValueError as exc:
        _fail("invalid_argument", str(exc))
        return
    except LookupError as exc:
        _fail("not_found", str(exc))
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"{agent}: {out['canary_percent']}% of new sessions start on Canary")


# -- ADR 0146 d5 / d6 / d2: compat, evidence pack, re-evaluation ---------------------------------

_EX_COMPAT = "Examples:\n\n  exa agent version compat jobdoc@Production jobdoc@Staging"
_EX_EVIDENCE = (
    "Examples:\n\n  exa agent version evidence jobdoc@Production --out jobdoc-evidence.json\n\n"
    "  exa -o json agent version evidence av-sha256:3f9a..."
)
_EX_REEVAL = (
    "Examples:\n\n  exa agent version reeval --status pending\n\n"
    "  exa agent version reeval --agent jobdoc"
)
_EX_REEVAL_RESOLVE = (
    "Examples:\n\n  exa agent version reeval-resolve 12 --outcome passed\n\n"
    "  exa agent version reeval-resolve 13 --outcome failed --reason 'tool accuracy -4pt'"
)


@version_app.command("compat", epilog=_EX_COMPAT)
def compat_cmd(
    running: str = typer.Argument(..., help="The running version (id or <agent>@<alias>)"),
    candidate: str = typer.Argument(..., help="The candidate version (id or <agent>@<alias>)"),
) -> None:
    """State-compatibility verdict between two versions: compatible, incompatible or inert."""
    from examlops.agent_versions.compat import state_compat

    a, b = av.get(running), av.get(candidate)
    missing = [r for r, row in ((running, a), (candidate, b)) if row is None]
    if missing:
        _fail("not_found", f"unknown agent version: {', '.join(missing)}")
        return
    out = state_compat(a["manifest"].get("state"), b["manifest"].get("state"))  # type: ignore[index]
    out = {"from": a["version_id"], "to": b["version_id"], **out}  # type: ignore[index]
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"{out['outcome']}: {out['from']} -> {out['to']}")
    for r in out["reasons"]:
        typer.echo(f"  - {r}")


@version_app.command("evidence", epilog=_EX_EVIDENCE)
def evidence_cmd(
    ref: str = typer.Argument(..., help="version id or <agent>@<alias>"),
    out: Path | None = typer.Option(None, "--out", help="Write the evidence pack JSON here"),
) -> None:
    """Export a version's evidence pack: tuple, evals, judges, grants, moves, audit (read-only)."""
    import json as _json

    from examlops.agent_versions.evidence_pack import export_evidence_pack

    try:
        pack = export_evidence_pack(ref)
    except LookupError as exc:
        _fail("not_found", str(exc))
        return
    if out is not None:
        try:
            out.write_text(_json.dumps(pack, indent=2, sort_keys=True, default=str) + "\n")
        except OSError as exc:
            _fail("write_failed", f"cannot write {out}: {exc}")
            return
    if _output.json_mode:
        _output.print_json(pack)
        return
    _output.ok(
        f"{pack['version']['agent']} {pack['version']['version_id']}: "
        f"{len(pack['evaluation']['results'])} eval result(s), "
        f"{len(pack['promotions_and_rollbacks'])} move(s), {len(pack['audit']['events'])} audit "
        f"event(s) - {pack['digest']}" + (f" -> {out}" if out else "")
    )


@version_app.command("reeval", epilog=_EX_REEVAL)
def reeval_cmd(
    agent: str | None = typer.Option(None, "--agent", "-a", help="Only this agent"),
    status: str | None = typer.Option(None, "--status", help="pending | passed | failed"),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=1000, help="Max entries"),
) -> None:
    """Re-evaluations enqueued because a model an agent follows was promoted (ADR 0146 d2)."""
    from examlops.agent_versions.reeval import list_reevals

    rows = list_reevals(agent=agent, status=status, limit=limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No re-evaluations")
        return
    _output.print_table(
        "Agent re-evaluations",
        ["id", "agent", "version_id", "model", "status", "blocking"],
        [
            [
                str(r["id"]),
                r["agent"],
                r["version_id"],
                f"{r['servable']}@{r['alias']} {r['previous_version']}->{r['model_version']}",
                r["status"],
                "yes" if r["blocking"] else "no",
            ]
            for r in rows
        ],
    )


@version_app.command("reeval-resolve", epilog=_EX_REEVAL_RESOLVE)
def reeval_resolve_cmd(
    reeval_id: int = typer.Argument(..., help="Re-evaluation id (from `exa agent version reeval`)"),
    outcome: str = typer.Option(..., "--outcome", help="passed | failed"),
    reason: str | None = typer.Option(None, "--reason", help="Why (recorded in the audit log)"),
) -> None:
    """Close a pending re-evaluation; `passed` lifts a blocking agent's model pin."""
    from examlops.agent_versions.reeval import resolve

    try:
        out = resolve(reeval_id, outcome, reason=reason)
    except ValueError as exc:
        _fail("invalid_argument", str(exc))
        return
    except LookupError as exc:
        _fail("not_found", str(exc))
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"re-evaluation {reeval_id}: {outcome}")


# -- ADR 0144: the agent runtime's control-plane side -------------------------------------------

runtime_app = typer.Typer(
    help="Agent runtime - compile its snapshot, serve it, inspect sandbox isolation (ADR 0144/0145)",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings=_H,
)

_EX_SNAPSHOT = (
    "Examples:\n\n  exa agent runtime snapshot --out /state/agent-snapshot.json\n\n"
    "  exa -o json agent runtime snapshot"
)
_EX_SANDBOXES = (
    "Examples:\n\n  exa agent runtime sandboxes\n\n  exa -o json agent runtime sandboxes"
)


@runtime_app.command("snapshot", epilog=_EX_SNAPSHOT)
def runtime_snapshot_cmd(
    out: Path | None = typer.Option(None, "--out", help="Write the snapshot here (atomically)"),
) -> None:
    """Compile the agent snapshot the runtime serves from (versions, aliases, grants, quotas)."""
    from examlops.agent_runtime.snapshot import compile_agent_snapshot, write_snapshot

    try:
        doc = compile_agent_snapshot()
    except ValueError as exc:
        _fail("invalid_configuration", str(exc))
        return
    if out is not None:
        try:
            write_snapshot(doc, out)
        except OSError as exc:
            _fail("write_failed", f"cannot write {out}: {exc}")
            return
    if _output.json_mode:
        _output.print_json(doc)
        return
    _output.ok(
        f"agent snapshot {doc['generation']} ({doc['digest']}): {len(doc['agents'])} agent(s), "
        f"{len(doc['versions'])} version(s)" + (f" -> {out}" if out else "")
    )


@runtime_app.command("sandboxes", epilog=_EX_SANDBOXES)
def runtime_sandboxes_cmd() -> None:
    """The sandbox isolation each substrate provider offers on this host (measured, not assumed)."""
    import shutil

    from examlops.agent_runtime.sandbox import ApptainerProvider, DockerProvider

    rows = []
    for provider, binary in ((DockerProvider(), "docker"), (ApptainerProvider(), "apptainer")):
        present = shutil.which(binary) is not None
        iso = provider.capabilities().isolation if present else "none"
        rows.append(
            {
                "provider": provider.name,
                "substrate": provider.substrate,
                "installed": present,
                "isolation": iso,
            }
        )
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        "Agent sandbox providers",
        ["provider", "substrate", "installed", "isolation"],
        [[r["provider"], r["substrate"], str(r["installed"]), r["isolation"]] for r in rows],
    )


_EX_SERVE = (
    "Examples:\n\n  exa agent runtime snapshot --out /state/agent-snapshot.json\n\n"
    "  exa agent runtime serve --snapshot /state/agent-snapshot.json\n\n"
    "  exa agent runtime serve --snapshot s.json --worker w1 --peer w1 --peer w2\n\n"
    "Callers authenticate with the bearer tokens in EXAMLOPS_AGENT_RUNTIME_TOKENS "
    '(a JSON map {"<token>": {"subject": ..., "tenant": ...}}); unset, every request is refused.'
)


@runtime_app.command("serve", epilog=_EX_SERVE)
def runtime_serve_cmd(
    snapshot: Path | None = typer.Option(
        None,
        "--snapshot",
        help="Agent snapshot file to serve from and follow (default: $EXAMLOPS_AGENT_SNAPSHOT)",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (loopback by default)"),
    port: int = typer.Option(18005, "--port", min=1, max=65535, help="Listen port"),
    state_db: str | None = typer.Option(
        None, "--state-db", help="Agent state store (default: $EXAMLOPS_AGENT_STATE_DB)"
    ),
    worker: str | None = typer.Option(None, "--worker", help="This worker's id (affinity)"),
    peer: list[str] = typer.Option(
        [], "--peer", help="Every worker id in the pool, this one included (repeatable)"
    ),
    interval: float = typer.Option(
        15.0,
        "--interval",
        min=0.05,
        envvar="EXAMLOPS_AGENT_RUNTIME_INTERVAL",
        help="Seconds between snapshot/sweep/recovery passes",
    ),
    allow_remote: bool = typer.Option(
        False, "--allow-remote", help="Allow binding beyond loopback (put TLS in front)"
    ),
) -> None:
    """Run the agent runtime (threads/runs HTTP surface) and its maintenance loop."""
    from examlops.agent_runtime.service import serve, snapshot_path_from_env

    path = snapshot or snapshot_path_from_env()
    if path is None:
        _fail("no_snapshot", "give --snapshot or set EXAMLOPS_AGENT_SNAPSHOT")
        return
    if peer and not worker:
        _fail("bad_peers", "--peer needs --worker (this worker's own id)")
        return
    if peer and worker not in peer:
        _fail("bad_peers", f"--worker {worker!r} must be one of the --peer ids")
        return
    try:
        serve(
            host=host,
            port=port,
            snapshot_path=path,
            state_db=state_db,
            worker_id=worker,
            peers=tuple(peer),
            interval=interval,
            allow_remote=allow_remote,
        )
    except ValueError as exc:
        _fail("refused", str(exc))
    except ImportError as exc:
        _fail("missing_dependency", f"{exc} (install fastapi and uvicorn)")
