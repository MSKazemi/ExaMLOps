"""`exa genai-app` - a GenAI application is one composed, versioned manifest (ADR 0159).

A version pins the composition - which gateway route, which knowledge base, which prompt, which
guardrail policy - as one content-addressed artifact, so the *combination* is versioned rather
than only its four ingredients. ``promote ... Production`` runs the platform's one evaluation gate
plus two refusals specific to a published application surface: it may not carry
``guardrail.mode: off``, and every component it declares must currently resolve.

Nothing here calls a model: this is the registry.

Exit codes: 0 ok, 1 refused (invalid manifest, unknown version, policy or promotion gate).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from examlops.cli import _output
from examlops.cli._policy_gate import enforce_and_confirm

_H = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(
    help="GenAI applications - composed, versioned route+RAG+prompt+guardrail manifests (ADR 0159)",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings=_H,
)

_EX_REGISTER = (
    "Examples:\n\n  exa genai-app register ./hpc-docs-assistant.yaml\n\n"
    "  exa -o json genai-app register ./hpc-docs-assistant.json"
)
_EX_SHOW = (
    "Examples:\n\n  exa genai-app show hpc-docs-assistant@Production\n\n"
    "  exa genai-app show gaa-sha256:3f9a..."
)
_EX_LIST = "Examples:\n\n  exa genai-app list\n\n  exa genai-app list --name hpc-docs-assistant"
_EX_PROMOTE = (
    "Examples:\n\n  exa genai-app promote hpc-docs-assistant Staging gaa-sha256:3f9a...\n\n"
    "  exa genai-app promote hpc-docs-assistant Production hpc-docs-assistant@Staging"
    " --reason 'evals green'\n\n"
    "Production needs recorded evaluation evidence (see `exa eval gate set --help`), a guardrail "
    "mode other than 'off', and every declared component to resolve."
)
_EX_INVOKE = (
    "Examples:\n\n  exa genai-app invoke hpc-docs-assistant --message 'how do I submit a job?'\n\n"
    "  exa genai-app invoke hpc-docs-assistant@Staging --message 'hello' --json\n\n"
    "Makes a real, billed call through the resolved route — same tier as `exa gateway chat`."
)


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


@app.command("register", epilog=_EX_REGISTER)
def register_cmd(
    file: Path = typer.Argument(..., help="Manifest file (JSON or YAML)", exists=False),
) -> None:
    """Validate a manifest and register it; identical content returns the existing version."""
    from examlops import genai_apps as ga

    doc = _load(file)
    try:
        out = ga.register(doc)
    except ga.GenAIAppManifestError as exc:
        _fail("invalid_manifest", "invalid genai application manifest", problems=exc.problems)
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    note = "registered" if out["created"] else "already registered (identical content)"
    _output.ok(f"{out['name']} {out['version_id']} {note}")


def _record(row: dict[str, Any]) -> dict[str, Any]:
    m = row["manifest"]
    rag = m.get("rag") or {}
    prompt = m["prompt"]
    pin = f"@{prompt['label']}" if "label" in prompt else f"@v{prompt['version']}"
    return {
        "version_id": row["version_id"],
        "name": row["name"],
        "route": m["route"]["model"],
        "key_ref": m["route"]["key_ref"],
        "rag": f"{rag['kb']} ({rag['retrieval']}, top_k={rag['top_k']})" if rag else "—",
        "prompt": f"{prompt['name']}{pin}",
        "guardrail": f"{m['guardrail']['mode']} / {m['guardrail']['policy']}",
        "eval_suites": ", ".join((m.get("eval") or {}).get("suites", [])) or "—",
    }


@app.command("show", epilog=_EX_SHOW)
def show_cmd(ref: str = typer.Argument(..., help="version id, or <name>@<alias>")) -> None:
    """Show one version: the composed route, RAG, prompt and guardrail references."""
    from examlops import genai_apps as ga

    row = ga.get(ref)
    if row is None:
        _fail("not_found", f"unknown genai application version {ref!r}")
        return
    if _output.json_mode:
        _output.print_json(
            {
                "version_id": row["version_id"],
                "name": row["name"],
                "manifest": row["manifest"],
                "created_at": row["created_at"],
                "actor": row["actor"],
            }
        )
        return
    _output.print_record(_record(row))


@app.command("list", epilog=_EX_LIST)
def list_cmd(
    name: str | None = typer.Option(None, "--name", "-a", help="Only this application"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500, help="Max versions"),
) -> None:
    """List registered versions, newest first, with the aliases pointing at each."""
    from examlops import genai_apps as ga

    rows = ga.list_apps(name, limit=limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No GenAI applications registered")
        return
    _output.print_table(
        "GenAI applications",
        ["version_id", "name", "aliases", "route", "rag", "guardrail"],
        [
            [
                r["version_id"],
                r["name"],
                ",".join(r["aliases"]),
                r["route"],
                r["rag"] or "—",
                r["guardrail"],
            ]
            for r in rows
        ],
    )


@app.command("promote", epilog=_EX_PROMOTE)
def promote_cmd(
    name: str = typer.Argument(..., help="Application name"),
    alias: str = typer.Argument(..., help="Staging | Canary | Production"),
    ref: str = typer.Argument(..., help="version id, or <name>@<alias> to copy an alias"),
    reason: str | None = typer.Option(None, "--reason", help="Why (recorded in the history)"),
) -> None:
    """Point an alias at a version. Production is gated on evidence, guardrails and resolution."""
    from examlops import genai_apps as ga

    try:
        canon = ga.canonical_alias(alias)
    except ValueError as exc:
        _fail("invalid_alias", str(exc))
        return
    ctx = {"application": name, "to_alias": canon, "alias": canon, "ref": ref}
    if not enforce_and_confirm(
        "genai_app_promote",
        ctx,
        what=f"moving {name}@{canon}",
        prompt=f"Move {name}@{canon}?",
    ):
        return
    try:
        out = ga.set_alias(name, canon, ref, reason=reason)
    except ga.GateRefusal as exc:
        _fail("promotion_refused", f"promotion refused: {exc}", reasons=exc.reasons)
        return
    except LookupError as exc:
        _fail("not_found", str(exc))
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"{name}@{canon} -> {out['version_id']} (was {out['previous'] or 'unset'})")


@app.command("invoke", epilog=_EX_INVOKE)
def invoke_cmd(
    ref: str = typer.Argument(
        ..., help="Application name, <name>@<alias> (default Production), or a version id"
    ),
    message: str = typer.Option(..., "--message", help="User message"),
) -> None:
    """Make one real, billed chat call through the resolved route (guardrail + optional RAG)."""
    from examlops import genai_apps as ga
    from examlops.gateway import GatewayError
    from examlops.structured import StructuredOutputError

    try:
        out = ga.invoke(ref, message)
    except ga.InvokeError as exc:
        _fail(exc.code, str(exc))
        return
    except (GatewayError, StructuredOutputError) as exc:
        # The gateway's own typed error, unchanged (ADR 0156 d1) — never re-wrapped as an
        # InvokeError, which names only a resolution-layer failure that never reached it. This
        # in-process hierarchy is distinguished by exception type, not a `.kind` string field
        # (that's the deployed service's `ProviderError` shape) — the class name is the code.
        _fail(type(exc).__name__, str(exc))
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    rag_note = (
        f" [rag: {out['rag']['kb']}, {len(out['rag']['citations'])} citations]"
        if out["rag"]
        else ""
    )
    tag = " (cached)" if out["cached"] else ""
    _output.ok(f"[{out['model']}] {out['reply']}{rag_note}  (cost ${out['cost_usd']:.6f}){tag}")
