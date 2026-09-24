"""ADR 0147 d1 — the agent surface is *generated* from the CLI, never hand-written twice.

Two hand-maintained surfaces drift. The ``exa`` CLI already carries everything an MCP tool
definition needs, in one place each:

* the **live Click tree** — every leaf command, its parameters, their types, choices,
  required-ness, defaults and help (walked by :func:`examlops.cli.surface.build_catalog`, the
  same walk ``exa docs`` and the dashboard CLI Console use);
* :mod:`examlops.cli.surface` — the authoritative per-command **tier** table built for ADR 0119
  (``read`` / ``admin`` / ``destructive`` / ``cli_only``), plus the blocked parameters, forced
  arguments and path-containment rules that make running a command from a remote caller safe.

This module joins the two and emits :class:`~examlops.mcp.tools.ToolSpec`-compatible entries:

* **input schema** from the declared parameters (JSON Schema; ``enum`` where the CLI declares
  choices, ``array`` where it declares ``multiple``/``nargs``);
* **``outputSchema``** from the command's JSON contract — ``exa --output json`` prints exactly
  one JSON document (``examlops.cli._output.print_json`` + ``install_structured_guard``), a
  contract machine-verified for every read command by ``tests/unit/test_cli_json_contract.py``;
* **annotations** from the tier — ``read`` → ``readOnlyHint``, ``destructive`` →
  ``destructiveHint``, :data:`examlops.cli.surface.IDEMPOTENT` → ``idempotentHint``,
  network reach → ``openWorldHint`` — produced by ``ToolSpec.annotations`` itself, so a
  generated tool and a hand-written one can never annotate the same fact differently.

**Opt-in.** Nothing here is registered unless ``EXAMLOPS_MCP_GENERATED_TOOLS`` is truthy; with
the flag off :data:`examlops.mcp.tools.REGISTRY` and ``iter_tools()`` are byte-identical to the
hand-written surface. Hand-written ``ToolSpec``s remain the home of **workflow-level** tools
(ADR 0147 d3) that consolidate several commands; generated names are prefixed
:data:`NAME_PREFIX` so the two sets can never collide.

**The guard.** :func:`build_spec` raises :class:`GenerationError` for a command with no tier or
no JSON contract, and :func:`refusals` names every command the generator declines and why — so a
command can never reach an agent without both a tier and a schema.
"""

from __future__ import annotations

import inspect
import json
import keyword
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from examlops.cli import surface

#: Env flag that turns the generated surface on. Off by default (this repo's convention for a
#: new surface): the hand-written tool set must stay byte-identical until a site opts in.
ENV_FLAG = "EXAMLOPS_MCP_GENERATED_TOOLS"

#: Containment root for a generated tool's filesystem parameters (``surface.contain_path``).
ENV_WORKSPACE = "EXAMLOPS_MCP_GENERATED_WORKSPACE"

#: Every generated tool name starts here, so it can never collide with a workflow tool.
NAME_PREFIX = "exa_"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: How long a generated tool waits for the CLI subprocess before giving up.
TIMEOUT_S = 120

# surface tier → (MCP write tier (ADR 0102), mutating, destructive). ``read`` tools carry the
# ``read`` tier; ``admin`` is a human-in-the-loop write (B); ``destructive`` is human-CLI-only (C),
# matching the hand-written registry's own vocabulary.
_TIER_MAP: dict[str, tuple[str, bool, bool]] = {
    surface.READ: ("read", False, False),
    surface.ADMIN: ("B", True, False),
    surface.DESTRUCTIVE: ("C", True, True),
}

_JSON_TYPES = {
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "choice": "string",
    "string": "string",
}

# `exa --help` panel → the lifecycle use case the capabilities catalogue files the tool under.
_PANEL_USE_CASES: dict[str, tuple[str, ...]] = {
    "Getting Started": ("help",),
    "Monitoring & Quality": ("monitoring",),
    "HPC, Fleet & FinOps": ("finops",),
    "Governance & Security": ("governance",),
}
_DEFAULT_USE_CASES = ("management",)


class GenerationError(RuntimeError):
    """A command cannot be turned into a tool — no tier, or no declared JSON contract."""


#: ``tool name → {"inputSchema", "outputSchema"}`` for every spec :func:`build_spec` has produced.
#: ``ToolSpec`` carries no schema fields (FastMCP derives the input schema from the signature);
#: a consumer that wants the declared JSON Schemas — the A2A card, a ``tools/list`` that
#: advertises ``outputSchema`` — reads them here.
SCHEMAS: dict[str, dict[str, Any]] = {}


# ── naming ────────────────────────────────────────────────────────────────────


def tool_name(path: str) -> str:
    """``"serve traffic"`` → ``"exa_serve_traffic"`` — a stable, collision-free tool name."""
    slug = "".join(ch if ch.isalnum() else "_" for ch in path)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return NAME_PREFIX + slug.strip("_")


def _arg_name(name: str) -> str:
    """A Click parameter name as a Python identifier (keywords get a trailing underscore)."""
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    if not safe or safe[0].isdigit():
        safe = f"p_{safe}"
    return f"{safe}_" if keyword.iskeyword(safe) else safe


# ── schemas ───────────────────────────────────────────────────────────────────


def _exposed_params(command: dict[str, Any]) -> list[dict[str, Any]]:
    """Parameters an agent may supply: not blocked, not implied, not the help flag."""
    return [
        p
        for p in command["params"]
        if not p.get("blocked") and not p.get("implied") and p["name"] != "help"
    ]


def _param_schema(param: dict[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = {"type": _JSON_TYPES.get(param.get("type", "string"), "string")}
    if param.get("flag"):
        base = {"type": "boolean"}
    if param.get("choices"):
        base["enum"] = list(param["choices"])
    for bound, key in (("min", "minimum"), ("max", "maximum")):
        if param.get(bound) is not None:
            base[key] = param[bound]
    if param.get("multiple") or int(param.get("nargs", 1) or 1) != 1:
        base = {"type": "array", "items": base}
    desc = param.get("help", "")
    if param.get("path"):
        note = "A path relative to the generated-tool workspace; absolute paths are refused."
        desc = f"{desc} {note}".strip()
    if param.get("secret"):
        desc = f"{desc} (secret — never echoed back)".strip()
    if desc:
        base["description"] = desc
    if not param.get("required") and param.get("default") is not None:
        base["default"] = param["default"]
    return base


def input_schema(command: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema for one command's declared parameters."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for param in _exposed_params(command):
        arg = _arg_name(param["name"])
        props[arg] = _param_schema(param)
        if param.get("required"):
            required.append(arg)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = sorted(required)
    return schema


def has_json_contract(command: dict[str, Any]) -> bool:
    """Whether ``exa <command> --output json`` prints exactly one JSON document.

    True for every command the platform allows to run non-interactively. A ``cli_only`` command
    is excluded by construction: those are the streaming/interactive ones (``--follow``, a device
    -code login, a token printed to a terminal) that have no single-document form at all —
    ``surface.CLI_ONLY_REASONS`` says so command by command.
    """
    return command.get("tier") in _TIER_MAP


def output_schema(command: dict[str, Any]) -> dict[str, Any]:
    """The command's declared JSON contract as an MCP ``outputSchema`` (``{}`` when it has none).

    MCP requires ``structuredContent`` to be an object, so the runner lifts a document that is a
    JSON array under ``items`` and a bare scalar under ``result`` — the same shape
    ``_output.merge_documents`` already uses for a multi-document command.
    """
    if not has_json_contract(command):
        return {}
    return {
        "type": "object",
        "description": (
            f"The single JSON document `exa {command['path']} --output json` prints. Command data "
            "keys are command-specific; the status keys below are the CLI's shared envelope."
        ),
        "properties": {
            "ok": {"type": "boolean", "description": "Present when the command reports success."},
            "message": {"type": "string", "description": "Human-readable success message."},
            "error": {"type": "string", "description": "Present when the command failed."},
            "exit_code": {"type": "integer", "description": "Process exit code on failure."},
            "hint": {"type": "string", "description": "Suggested next step on failure."},
            "items": {
                "type": "array",
                "description": "Present when the command's document is a JSON array.",
            },
            "result": {"description": "Present when the command's document is a bare scalar."},
        },
        "additionalProperties": True,
    }


# ── the runner ────────────────────────────────────────────────────────────────


def workspace_root() -> Path:
    """Directory every filesystem parameter of a generated tool is contained inside."""
    override = os.getenv(ENV_WORKSPACE, "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        from examlops.lifecycle import datadir

        data = datadir.data_path("mcp-workspace")
        if data is not None:
            root = data
        else:
            share = os.getenv("XDG_DATA_HOME", "").strip() or str(Path.home() / ".local/share")
            root = Path(share) / "examlops" / "mcp-workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _err(message: str, **extra: Any) -> dict[str, Any]:
    from examlops.sdk import err

    return err(message, **extra).to_dict()


def _as_object(document: Any) -> dict[str, Any]:
    if isinstance(document, dict):
        return document
    if isinstance(document, list):
        return {"items": document}
    return {"result": document}


def run_command(command: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Run one ``exa`` command in a subprocess and return its JSON document as an object.

    Never raises: an invalid argument, a crash or unparsable output all come back as the CLI's
    own ``{"ok": false, "error": …}`` envelope, which is what lets an agent reason about the
    failure instead of losing the turn to an exception.
    """
    try:
        invocation = surface.build_argv(command, values, workspace=workspace_root())
    except surface.SurfaceError as exc:
        return _err(str(exc), command=command["path"])
    except Exception as exc:  # noqa: BLE001 - a generated tool must never raise at its caller
        return _err(f"could not build the command: {exc}", command=command["path"])
    argv = [sys.executable, "-m", "examlops.cli", "--output", "json", "--yes", *invocation.argv]
    try:
        done = subprocess.run(  # noqa: S603 - argv is built and validated by `surface.build_argv`
            argv,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return _err(f"`exa {command['path']}` timed out after {TIMEOUT_S}s")
    except Exception as exc:  # noqa: BLE001 - defensive; the tool contract is "never raise"
        return _err(f"`exa {command['path']}` could not be run: {exc}")
    text = (done.stdout or "").strip()
    try:
        document = json.loads(text)
    except ValueError:
        detail = (done.stderr or text or "").strip()[:500]
        return _err(
            f"`exa {command['path']}` printed no JSON document",
            exit_code=done.returncode,
            detail=detail,
        )
    return _as_object(document)


def _make_fn(command: dict[str, Any], name: str, description: str) -> Any:
    """A keyword-only callable whose signature mirrors the command's declared parameters."""
    params = _exposed_params(command)
    by_arg = {_arg_name(p["name"]): p for p in params}

    def call(**kwargs: Any) -> dict[str, Any]:
        values = {by_arg[k]["name"]: v for k, v in kwargs.items() if k in by_arg}
        return run_command(command, values)

    signature: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for arg, param in by_arg.items():
        annotation = _annotation(param)
        annotations[arg] = annotation
        signature.append(
            inspect.Parameter(
                arg,
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if param.get("required") else None,
                annotation=annotation,
            )
        )
    # Required parameters first: a Signature rejects a defaulted keyword-only before a required
    # one only for positionals, but ordering them anyway keeps `--help`-like renderings readable.
    signature.sort(key=lambda p: p.default is not inspect.Parameter.empty)
    call.__name__ = name
    call.__qualname__ = name
    call.__doc__ = description
    call.__signature__ = inspect.Signature(signature)  # type: ignore[attr-defined]
    call.__annotations__ = {**annotations, "return": dict[str, Any]}
    return call


def _annotation(param: dict[str, Any]) -> Any:
    base: Any = str
    if param.get("flag"):
        base = bool
    else:
        base = {"int": int, "float": float, "bool": bool}.get(param.get("type", "string"), str)
    if param.get("multiple") or int(param.get("nargs", 1) or 1) != 1:
        base = list[base]  # type: ignore[valid-type]
    return base if param.get("required") else (base | None)


# ── generation ────────────────────────────────────────────────────────────────


def _description(command: dict[str, Any]) -> str:
    text = command.get("short_help") or command.get("help") or ""
    text = text.split("\n\n", 1)[0].replace("\n", " ").strip()
    return f"`exa {command['path']}` — {text}" if text else f"`exa {command['path']}`"


def build_spec(command: dict[str, Any], *, tier: str | None = None) -> Any:
    """One command → a :class:`~examlops.mcp.tools.ToolSpec`.

    Args:
        command: A :func:`examlops.cli.surface.build_catalog` descriptor.
        tier: The command's surface tier. Defaults to the descriptor's own ``tier``; pass
            ``None`` explicitly for a command the table does not classify.

    Raises:
        GenerationError: the command has no tier, or no declared JSON contract. This is the
            ADR's guard: a command can never reach an agent without both.
    """
    from examlops.mcp.tools import ToolSpec

    path = command["path"]
    resolved = command.get("tier") if tier is None else tier
    if resolved == surface.CLI_ONLY:
        raise GenerationError(
            f"`exa {path}` declares no JSON output contract: "
            f"{surface.CLI_ONLY_REASONS.get(path) or 'cli_only'}"
        )
    mapped = _TIER_MAP.get(resolved or "")
    if mapped is None:
        raise GenerationError(
            f"`exa {path}` has no agent tier ({resolved!r}); classify it in "
            "examlops.cli.surface.TIERS before exposing it to agents"
        )
    schema_out = output_schema({**command, "tier": resolved})
    if not schema_out:  # pragma: no cover - unreachable once the tier is mapped; belt and braces
        raise GenerationError(f"`exa {path}` declares no JSON output contract")
    schema_in = input_schema(command)
    mcp_tier, mutating, destructive = mapped
    name = tool_name(path)
    spec = ToolSpec(
        _make_fn(command, name, _description(command)),
        mutating=mutating,
        tags=("write" if mutating else "read", command["group"]),
        use_cases=_PANEL_USE_CASES.get(command.get("panel", ""), _DEFAULT_USE_CASES),
        tier=mcp_tier,
        idempotent=mutating and path in surface.IDEMPOTENT,
        destructive=destructive,
        # Conservative and in MCP's own default direction: `openWorldHint` true means the tool
        # may reach an entity outside this process. Every `exa` command may talk to the control
        # plane, MLflow or a cluster, and a command that declares a network-target parameter
        # certainly does — claiming a closed world would be the dangerous way to be wrong.
        open_world=True,
    )
    SCHEMAS[name] = {"inputSchema": schema_in, "outputSchema": schema_out}
    return spec


def refusals(catalog: dict[str, Any] | None = None) -> dict[str, str]:
    """Every command the generator declines to expose, and why — the guard's report."""
    cat = catalog if catalog is not None else surface.build_catalog()
    unclassified = set(cat.get("unclassified", ()))
    out: dict[str, str] = {}
    for command in cat["commands"]:
        path = command["path"]
        if path in unclassified:
            out[path] = "no tier in examlops.cli.surface.TIERS"
        elif command["tier"] == surface.CLI_ONLY:
            out[path] = surface.CLI_ONLY_REASONS.get(path) or "cli_only: no JSON output contract"
    return out


def generate(
    *,
    include_writes: bool | None = None,
    disabled_modules: Iterable[str] = (),
    catalog: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Every exposable ``exa`` command as a ``ToolSpec``, in a stable (path-sorted) order.

    Args:
        include_writes: Include mutating tools. ``None`` => the ``EXAMLOPS_MCP_ALLOW_WRITES``
            decision the caller already made (pass it explicitly from ``iter_tools``).
        disabled_modules: Site-profile modules switched off (ADR 0128); a command whose root
            command belongs to one is not offered at all.
        catalog: A pre-built :func:`examlops.cli.surface.build_catalog` (tests and callers that
            already hold one); built fresh otherwise, so commands another package registers are
            picked up at runtime rather than snapshotted.
    """
    if include_writes is None:
        from examlops.mcp.tools import _writes_enabled

        include_writes = _writes_enabled()
    cat = catalog if catalog is not None else surface.build_catalog()
    declined = refusals(cat)
    off = set(disabled_modules)
    specs: list[Any] = []
    for command in sorted(cat["commands"], key=lambda c: c["path"]):
        if command["path"] in declined:
            continue
        spec = build_spec(command)
        if spec.mutating and not include_writes:
            continue
        if off and _module_for(command["group"]) in off:
            continue
        specs.append(spec)
    return tuple(specs)


def _module_for(group: str) -> str | None:
    try:
        from examlops.lifecycle.modules import module_for_command

        return module_for_command(group)
    except Exception:  # noqa: BLE001 - a broken profile must not take the agent surface down
        return None


def enabled() -> bool:
    """Whether the generated surface is switched on (``EXAMLOPS_MCP_GENERATED_TOOLS``)."""
    return os.getenv(ENV_FLAG, "").strip().lower() in _TRUTHY
