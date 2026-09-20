"""The `exa` command surface as data — what every command is, who may run it remotely, and how.

The dashboard's CLI Console (ADR 0119) renders a form for every `exa` command and runs the real
CLI in a subprocess. That is only safe if one place decides, for every leaf command, what a
browser may do with it. This module is that place:

* :data:`TIERS` — every leaf command is exactly one of ``read`` (any signed-in user), ``admin``
  (admin role — mutations and sensitive reads), ``destructive`` (admin plus a typed confirmation)
  or ``cli_only`` (never run from a browser; :data:`CLI_ONLY_REASONS` says why and where to go
  instead). ``tests/unit/test_cli_surface.py`` fails when a command is added to the CLI without
  an entry here, so the parity gap cannot silently reopen and a new mutation cannot silently
  default to "anyone may run it".
* :func:`build_catalog` — the live Click tree, flattened to JSON-safe command descriptors
  (params with type/choices/required/default, help, examples, panel, tier).
* :func:`build_argv` — turns a ``{param: value}`` mapping into an argv list for one command,
  validating every value against the declared parameter, containing every filesystem path inside
  a workspace root, refusing blocked flags, and computing the *effective* tier (a read becomes
  admin when a persisting flag, a filesystem path or a network target is supplied).

Pure and side-effect free: building the catalog imports the CLI tree but runs no command. Nothing
here executes anything; the caller (the dashboard's runner) owns the subprocess.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

READ = "read"
ADMIN = "admin"
DESTRUCTIVE = "destructive"
CLI_ONLY = "cli_only"

_TIER_ORDER = {READ: 0, ADMIN: 1, DESTRUCTIVE: 2, CLI_ONLY: 3}

R, A, D, X = READ, ADMIN, DESTRUCTIVE, CLI_ONLY

# ── The tier of every leaf command ────────────────────────────────────────────────────────
# Keyed by the space-joined command path under `exa`. Reviewed by hand, command by command,
# against what the command actually does (not what its verb suggests): `data validate` and
# `cards dataset` persist a record, so they are admin; `pipeline quality check` only writes an
# audit event, so it stays a read. When in doubt the higher tier wins. Governance surfaces the
# dashboard already restricts to admins (audit, approvals, compliance, fairness, secrets, backups,
# Skipper memory) stay admin here too, so the console can never widen what a viewer sees.
TIERS: dict[str, str] = {
    "admission stats": R,
    "admission submit": A,
    "agent memory delete": D,
    "agent memory export": A,
    "agent memory list": A,
    "agent memory review approve": A,
    "agent memory review list": A,
    "agent memory review reject": A,
    "agent memory stats": R,
    "agent status": R,
    "agentops anomalies": R,
    "agentops replay": R,
    "agentops sessions": R,
    "agentops tools": R,
    "approvals approve": D,
    "approvals delete": D,
    "approvals list": A,
    "approvals reject": A,
    "commands cancel": A,
    "commands list": R,
    "commands show": R,
    "ask": R,
    "assets declare": A,
    "assets graph": R,
    "assets list": R,
    "assets materialize": A,
    "assets source-changed": A,
    "assets status": R,
    "audit anchor": A,
    "audit autonomy": A,
    "audit chain": A,
    "audit checkpoint": A,
    "audit checkpoints": A,
    "audit export": A,
    "audit review": A,
    "audit reviews": A,
    "audit verify": A,
    "audit verify-anchors": A,
    "audit verify-worm": A,
    # ADR 0120 identity federation. `whoami`/`status`/`providers` show identity, never a credential.
    # `verify`/`decide` read a token file and may reach the IdP/PDP → admin. `validate` reads the
    # trust file (issuers, client ids, PDP URLs) → admin. `login`/`token` → cli_only (reasons below).
    "auth accounts": A,  # lists people's usernames and emails — not a viewer read
    "auth activate": A,
    "auth deactivate": A,  # reversible (`auth activate`), audited
    "auth decide": A,
    "auth login": X,
    "auth logout": A,
    "auth providers": R,
    "auth status": R,
    "auth token": X,
    "auth validate": A,
    "auth verify": A,
    "auth whoami": R,
    "autopilot autonomy": A,
    "autopilot contract": A,
    "autopilot disable": A,
    "autopilot enable": A,
    "autopilot follow": X,
    "autopilot interrupt": D,
    "autopilot quarantine": D,
    "autopilot release": A,
    "autopilot resume": A,
    "autopilot run": D,
    "autopilot status": R,
    "backup create": A,
    "backup list": A,
    "backup prune": D,
    "backup pull": A,
    "backup restore": D,
    "backup restore-bundle": D,
    "backup schedule": A,
    "backup status": A,
    "backup verify": A,
    "backup verify-bundle": A,
    "cards completeness": R,
    "cards dataset": A,
    "cards export": R,
    "cards model": R,
    "chat": X,
    "compliance art12": A,
    "compliance classify": A,
    "compliance declaration": A,
    "compliance declare": A,
    "compliance framework": R,
    "compliance status": A,
    "compliance technical-file": A,
    "config contexts": R,
    "config delete-context": D,
    "config export": A,
    "config init": X,
    "config set": A,
    "config show": R,
    "config unset": A,
    "config use": A,
    "connection create": A,
    "connection delete": D,
    "connection list": R,
    "connection show": R,
    "connection test": A,
    "data checkout": A,
    "data diff": R,
    "data list": R,
    "data retention-prune": D,
    "data snapshot": A,
    "data synth evaluate": A,
    "data synth fit": A,
    "data synth generate": A,
    "data validate": A,
    # ADR 0130 dataplane. `test`/`preview` reach out with a connection's stored credentials (like
    # `connection test`); credentials only ever arrive through `--connection`, never inline.
    "dataplane catalog-rebuild": A,
    "dataplane connectors": R,
    "dataplane manifest": R,
    "dataplane preview": A,
    "dataplane prune": D,
    "dataplane pull": A,
    "dataplane pulls": R,
    "dataplane snapshots": R,
    "dataplane sources apply": A,
    "dataplane sources create": A,
    "dataplane sources delete": D,
    "dataplane sources list": R,
    "dataplane sources show": R,
    "dataplane test": A,
    "docs": R,
    "doctor": R,
    "drift auto-retrain disable": A,
    "drift auto-retrain enable": A,
    "drift auto-retrain status": R,
    "drift baseline": A,
    "drift concept": R,
    "drift consume-telemetry": X,
    "drift corruption baseline": A,
    "drift corruption classify": R,
    "drift corruption selftest": R,
    "drift corruption status": R,
    "drift estimate": R,
    "drift events": R,
    "drift forecast": R,
    "drift input baseline": A,
    "drift input reset": D,
    "drift input status": R,
    "drift profile": R,
    "drift reset": D,
    "drift snapshots": R,
    "drift status": R,
    "drift trigger": D,
    "embedding list": R,
    "embedding migrate": A,  # additive publish to MLflow: an encoder already there is skipped
    "embedding register": A,
    "embedding reindex": A,
    "embedding set-encoder": A,
    "embedding status": R,
    "env": R,
    "eval calibrate": A,
    "eval calibration list": R,
    "eval calibration show": R,
    "eval cli-coverage": A,
    "eval feedback accuracy": R,
    "eval feedback ingest": A,
    "eval feedback join": R,
    "eval gate run": A,
    "eval gate set": A,
    "eval gate show": R,
    "eval grounding": A,
    "eval history": R,
    "eval operator-qa": A,
    "eval run": A,
    "eval safety": A,
    "events publish": A,
    "events relay": A,
    "events stats": R,
    "events tail": R,
    "exchange import": D,
    "exchange inspect": A,
    "exchange pack": A,
    "exchange verify": A,
    "explain": R,
    "fairness apply": A,
    "fairness config": A,
    "fairness report": A,
    "fairness show": A,
    "fairness slice": A,
    "feature apply": A,
    "feature freshness": R,
    "feature get": R,
    "feature ingest": A,
    "feature list": R,
    "feature materialize": A,
    "feature similar": R,
    "feature skew": R,
    "features list": R,
    "features pull": A,
    "features push": A,
    "federated budget": R,
    "federated init": A,
    "federated round": A,
    "federated status": R,
    "finetune": A,
    "finops budget set": A,
    "finops budget status": R,
    "finops carbon estimate": R,
    "finops carbon policy evaluate": R,  # `--record` chains an audit event, escalating to admin
    "finops carbon policy list": R,
    "finops carbon policy sample": A,  # always writes its `--out` trace file
    "finops carbon policy status": R,
    "finops carbon providers": R,
    "finops carbon record": A,
    "finops carbon report": R,
    "finops carbon signal": R,
    "finops cost providers": R,
    "finops economics": R,
    "fleet heatmap": R,
    "fleet simulate": R,
    "gateway cache stats": R,
    "gateway chat": A,
    "gateway key issue": A,
    "gateway key list": A,
    "gateway key revoke": D,
    "gateway quota list": R,
    "gateway quota remove": D,
    "gateway quota set": A,
    "gateway reasoning account": A,
    "gateway reasoning budget": R,
    "gateway reasoning budgets": R,
    "gateway reasoning set-budget": A,
    "gateway reasoning stats": R,
    "gateway schema test": A,
    "genai check": R,
    "genai cost": R,
    "governance catalogue": R,
    "governance crosswalk": R,
    "governance report": A,
    "governance validate": R,
    "guardrails check-tool": R,
    "guardrails stats": R,
    "guardrails test": R,
    "hardware add-pool": A,
    "hardware burst": A,
    "hardware decisions": R,
    "hardware place": A,
    "hardware pools": R,
    "hardware portable": R,
    "hpc approve": D,
    "hpc capacity": R,
    "hpc clusters": R,
    "hpc connect": A,
    "hpc detect": A,
    "hpc gpu-share accounting": R,
    "hpc gpu-share pack": R,
    "hpc gpu-share plan": R,
    "hpc gpus": A,
    "hpc jobs": R,
    "hpc nodes": A,
    "hpc place": R,
    "hpc preflight": R,
    "hpc prometheus-sd": A,
    "hpc queue": R,
    "hpc reject": A,
    "instance check": R,
    "instance info": R,
    "instance init": X,
    "mcp agent-card": R,
    "mcp capabilities": R,
    "mcp prompts": R,
    "mcp resources": R,
    "mcp serve": X,
    "mcp tools": R,
    "models bom": R,
    "models card generate": A,
    "models card history": R,
    "models cost": R,
    "models cost-list": R,
    "models diff": R,
    "models engine list": R,
    "models engine validate": A,
    "models info": R,
    "models lineage": R,
    "models list": R,
    "models parity": R,
    "models quantize": A,
    "models rollback history": R,
    "models rollback run": D,
    "models sign": A,
    "models verify": A,
    "modelzoo adopt": A,
    "modelzoo config": R,
    "modelzoo config-set": A,
    "modelzoo events": R,
    "modelzoo status": R,
    "modelzoo sync": A,
    "modules disable": A,
    "modules enable": A,
    "modules list": R,
    "modules preset": A,
    "modules presets": R,
    "modules render": R,  # prints; `--out` writes a file, which escalates it to admin
    "modules reset": D,
    "modules show": R,
    "namespace assign": A,
    "namespace create": A,
    "namespace info": R,
    "namespace list": R,
    "pipeline add-model": A,
    "pipeline deploy": D,
    "pipeline distributed checkpoint": A,
    "pipeline distributed launch": A,
    "pipeline distributed list": R,
    "pipeline distributed resume": A,
    "pipeline distributed status": R,
    "pipeline export-registry": A,
    "pipeline hpo record": A,
    "pipeline hpo start": A,
    "pipeline hpo status": R,
    "pipeline list": R,
    "pipeline promote": D,
    "pipeline promote-delete": D,
    "pipeline quality check": R,
    "pipeline quality history": R,
    "pipeline run": A,
    "pipeline validate": R,
    "pipeline validate-model": A,
    "ops cancel": A,
    "ops list": R,
    "ops status": R,
    "ops wait": R,
    "plan list": A,
    "plan show": A,
    "plugins": R,
    "policy bundle list": R,
    "policy bundle sign": A,
    "policy bundle verify": R,
    "policy eval": A,
    "policy list": R,
    "policy simulate": R,  # pure evaluation, audit=False; exit 4 = require_approval
    "policy test": R,
    "predict": R,
    "production deploy": D,
    "production reload": A,
    "production verify": R,
    "project access": R,
    "project add-member": A,
    "project archive": D,
    "project assign": A,
    "project assign-model": A,
    "project budget": R,
    "project compose": R,
    "project cost": R,
    "project create": A,
    "project current": R,
    "project delete": D,
    "project grant": A,
    "project list": R,
    "project members": R,
    "project pipelines": R,
    "project remove-member": A,
    "project revoke": A,
    "project set-quota": A,
    "project show": R,
    "project storage": R,
    "project use": A,
    "prompt backend": R,
    "prompt create": A,
    "prompt diff": R,
    "prompt label": A,
    "prompt list": R,
    "prompt migrate": A,  # additive copy: a prompt that exists at the destination is skipped
    "prompt rollback": A,
    "prompt show": R,
    "providers activate": A,
    "providers author": A,
    "providers authored": R,
    "providers list": R,
    "providers rm": D,
    "providers show": R,
    "providers validate": A,
    "rag ingest": A,
    "rag list": R,
    "rag query": R,
    "report generate": R,
    "reproduce build": A,
    "reproduce list": R,
    "reproduce run": A,
    "reproduce verify": R,
    "retrain": A,
    "retrain-status": R,
    "scaffold": A,
    "dataplane-bus init-uuids": A,
    "dataplane-bus list": R,
    "dataplane-bus regen-uuid": A,
    "dataplane-bus status": R,
    "secrets get": A,
    "secrets list": A,
    "secrets rewrap": D,
    "secrets rotate": D,
    "secrets scan": A,
    "secrets set": A,
    "serve ab analyze": R,
    "serve ab record": A,
    "serve ab start": A,
    "serve ab status": R,
    "serve ab stop": A,
    "serve adapter add": A,
    "serve adapter list": R,
    "serve adapter promote": D,
    "serve adapter route": A,
    "serve autoscale record": A,
    "serve autoscale run": A,  # dry run by default, but `--apply` scales; the verb is mutating
    "serve autoscale savings": R,
    "serve autoscale set": A,
    "serve autoscale simulate": R,
    "serve autoscale status": R,
    "serve backend": R,
    "serve batch list": R,
    "serve batch submit": A,
    "serve benchmark": A,
    "serve challenger disable": A,
    "serve challenger enable": A,
    "serve challenger judge": A,
    "serve challenger list": R,
    "serve challenger promote": D,
    "serve challenger status": R,
    "serve check": R,
    "serve explain explain": R,
    "serve explain history": R,
    "serve infer-check": R,
    "serve loadtest": A,  # sends sustained traffic at the model server: an operator's decision
    "serve llm args": R,
    "serve llm bench": A,
    "serve llm chat": A,
    "serve llm health": R,
    "serve llm list": R,
    "serve llm start": A,
    "serve llm status": R,
    "serve llm stop": A,
    "serve manifest": R,
    "serve models": R,
    "serve reload": A,
    "serve routing set": A,
    "serve routing simulate": R,
    "serve routing stats": R,
    "serve shadow disable": A,
    "serve shadow enable": A,
    "serve shadow log": R,
    "serve shadow status": R,
    "serve snapshot publish": A,
    "serve snapshot show": R,
    "serve traffic": D,
    "serve traffic-list": R,
    "slo apply": A,
    "slo burn": R,
    "slo export-metrics": R,
    "slo generate": R,
    "slo ingest": A,
    "slo list": R,
    "slo pair-check": R,
    "slo pair-list": R,
    "slo pair-set": A,
    "slo record": A,
    "slo set": A,
    "slo status": R,
    "stack down": X,
    "stack logs": R,
    "stack monitoring-down": X,
    "stack monitoring-status": R,
    "stack monitoring-up": X,
    "stack restart": X,
    "stack status": R,
    "stack up": X,
    "status": R,
    "upgrade apply": X,
    "upgrade history": R,
    "upgrade plan": R,
    "vector create": A,
    "vector drop": D,
    "vector reindex": A,
    "vector search": R,
    "vector stats": R,
    "vector upsert": A,
    "workbench create": A,
    "workbench delete": D,
    "workbench list": R,
    "workbench start": A,
    "workbench stop": A,
}

# Why a command is never run from a browser, and what to use instead. Required for every
# ``cli_only`` entry (guarded) — "not available" with no reason reads as a missing feature.
CLI_ONLY_REASONS: dict[str, str] = {
    "auth login": "Interactive device sign-in: it shows a code to approve in a browser and waits "
    "for it. The dashboard has its own organisation sign-in; run `exa auth login` in a terminal.",
    "auth token": "Prints the caller's bearer credential. Run from the console it would print the "
    "dashboard server's own session token to whoever clicked; use `exa auth token` in a terminal.",
    "chat": "Interactive REPL that reads from a terminal. Use `ask` here (one question, "
    "optional --session) or the Copilot panel for a conversation.",
    "autopilot follow": "A long-running consumer of the event backbone that runs a model's "
    "autopilot cycle when its training run completes. Run it as a service on a host with "
    "`exa autopilot follow`; `autopilot run` is the one-shot equivalent.",
    "drift consume-telemetry": "A long-running consumer of the event backbone that writes "
    "drift/input-embedding snapshots published by a bridge running with "
    "EXAMLOPS_TELEMETRY_VIA_EVENTBUS=1. Run it as a service on a host with "
    "`exa drift consume-telemetry`; without that bridge mode there is nothing to consume.",
    "mcp serve": "Starts a long-running MCP server process; it is not a request/response "
    "command. Run it on a host with `exa mcp serve`; `mcp tools|resources|prompts` list "
    "what it would expose.",
    "config init": "Interactive setup wizard that prompts for each value. Use `config set` "
    "for one key at a time.",
    "stack up": "Starts the host's Docker Compose stack, which includes the container "
    "serving this dashboard. Per-service start/stop/restart is on the Services console.",
    "stack down": "Stops the host's Docker Compose stack — including this dashboard, which "
    "would end the session that issued it. Use the Services console per service.",
    "stack restart": "Restarts host stack services, including this dashboard. Use the "
    "Services console per service — for its own container it delays the restart and tells "
    "the page when to reconnect.",
    "stack monitoring-up": "Brings up the host monitoring stack with Docker Compose; run it "
    "on the host (`make monitoring-up`).",
    "stack monitoring-down": "Stops the host monitoring stack with Docker Compose; run it "
    "on the host (`make monitoring-down`).",
    "instance init": "Creates an instance-data root on the host, which every ExaMLOps process "
    "must then be started with (`EXAMLOPS_DATA_DIR`). Run it on the host. `instance info` and "
    "`instance check` show the current install here.",
    "upgrade apply": "Migrates the live datastore that this dashboard and every other service are "
    "running on. Run it from the host in a maintenance window. `upgrade plan` shows what it "
    "would do, and `upgrade history` shows what already ran.",
}

# Flags that are refused outright from a browser: they make a run endless (`--watch`,
# `--follow`) or put a secret value on the wire (`--reveal`). The dashboard never returns a
# secret value (D7); the Secrets console follows the same rule.
BLOCKED_PARAMS: dict[str, frozenset[str]] = {
    "status": frozenset({"watch", "interval"}),
    "drift status": frozenset({"watch", "interval"}),
    "serve traffic-list": frozenset({"watch", "interval"}),
    "stack logs": frozenset({"follow"}),
    "secrets get": frozenset({"reveal"}),
    "backup schedule": frozenset({"once", "interval"}),
    "serve autoscale run": frozenset({"once", "interval"}),
}

# Arguments always appended so a run terminates: `stack logs` follows by default and `backup
# schedule` loops forever without `--once`.
FORCED_ARGS: dict[str, tuple[str, ...]] = {
    "stack logs": ("--no-follow",),
    "backup schedule": ("--once",),
    "serve autoscale run": ("--once",),
}

# Values the command would otherwise *prompt* for. stdin is /dev/null in the console, so a prompt
# aborts the run; requiring the value in the form is the honest equivalent of answering it.
_CONSOLE_REQUIRED: frozenset[tuple[str, str]] = frozenset(
    {
        ("config set", "value"),  # hidden prompt for a secret key when omitted
        ("models rollback run", "version"),  # lists versions, then asks which one
    }
)

# Supplying any of these on a `read` command makes it persist something, so the run needs admin.
_PERSISTING_PARAMS = frozenset(
    {"record", "save", "push", "approve", "deny", "refresh", "bind_connection", "execute"}
)

# Parameter names that name a file or directory on the machine running the command.
_PATH_NAMES = frozenset(
    {
        "out",
        "output",
        "path",
        "directory",
        "dest",
        "pack_path",
        "bundle_dir",
        "file",
        "file_path",
        "files",
        "from_csv",
        "from_file",
        "input_file",
        "object_file",
        "schema_file",
        "registry",
        "registry_dir",
        "yaml_path",
        "docs",
        "items",
        "real",
        "synthetic",
    }
)
# Path-named parameters that are not filesystem paths (a secret's path in the store, an
# HF model id, a directory on the *serving* host that the launcher passes to vLLM).
_NOT_FS: frozenset[tuple[str, str]] = frozenset(
    {
        ("secrets get", "path"),
        ("secrets set", "path"),
        ("secrets rotate", "path"),
        ("serve llm start", "hf_model"),
        ("serve llm start", "local_media_path"),
        ("explain", "command"),
        ("feature apply", "source"),
    }
)
# Filesystem parameters whose name alone does not say so.
_EXTRA_FS: frozenset[tuple[str, str]] = frozenset(
    {
        ("slo pair-check", "samples"),
        ("secrets scan", "target"),
        ("hpc connect", "key"),
        ("hpc detect", "key"),
        ("hpc gpus", "key"),
        ("hpc nodes", "key"),
        ("serve llm chat", "image"),
        ("agent memory export", "out"),
        ("auth decide", "token_file"),
        ("auth verify", "token_file"),
        ("upgrade apply", "backup_dir"),
        ("finops carbon policy evaluate", "trace"),
        ("serve loadtest", "body"),
    }
)
# A path-or-URL parameter: an http(s) value passes through (and is a network target), anything
# else is contained like a path.
_PATH_OR_URL: frozenset[tuple[str, str]] = frozenset({("serve llm chat", "image")})

# Parameters that make the dashboard's process reach another machine.
_NETWORK_NAMES = frozenset({"agent_url", "base_url", "host", "user"})

# Values that must never reach an audit row or a run record in clear.
_SECRET_NAMES = frozenset({"secret_value", "token", "password", "api_key"})
_SECRET_PARAMS: frozenset[tuple[str, str]] = frozenset(
    {
        ("secrets set", "value"),
        ("config set", "value"),
        ("modelzoo config-set", "value"),
        ("gateway chat", "key"),
    }
)

_MAX_VALUE_LEN = 10_000
_MARKUP = re.compile(r"\[/?[a-z ]+\]")
_CONTEXT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class SurfaceError(ValueError):
    """A request that the command's declared surface does not allow (maps to HTTP 400/403)."""


# ── Catalog ───────────────────────────────────────────────────────────────────────────────


def _clean(text: str | None) -> str:
    return _MARKUP.sub("", text or "").strip()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    enum_value = getattr(value, "value", None)
    if enum_value is not None and isinstance(enum_value, (str, int, float)):
        return enum_value
    return str(value)


def _examples(epilog: str | None) -> list[dict[str, str]]:
    """``exa …`` lines from a command's epilog, each with the ``# comment`` just above it."""
    out: list[dict[str, str]] = []
    comment = ""
    for raw in _clean(epilog).splitlines():
        line = raw.strip()
        if line.startswith("#"):
            comment = line.lstrip("# ").strip()
        elif line.startswith("exa "):
            out.append({"cmd": line, "comment": comment})
            comment = ""
    return out[:8]


def _param_kind(path: str, name: str) -> dict[str, bool]:
    key = (path, name)
    is_path = (name in _PATH_NAMES or key in _EXTRA_FS) and key not in _NOT_FS
    return {
        "path": is_path,
        "path_or_url": key in _PATH_OR_URL,
        "network": name in _NETWORK_NAMES,
        "secret": name in _SECRET_NAMES or key in _SECRET_PARAMS,
        "persisting": name in _PERSISTING_PARAMS,
    }


def _describe_param(path: str, param: Any) -> dict[str, Any]:
    type_name = type(param.type).__name__
    if type_name in ("TyperChoice", "Choice"):
        ptype = "choice"
    elif type_name in ("IntParamType", "IntRange"):
        ptype = "int"
    elif type_name in ("FloatParamType", "FloatRange"):
        ptype = "float"
    elif type_name == "BoolParamType":
        ptype = "bool"
    else:
        ptype = "string"
    is_option = getattr(param, "param_type_name", None) == "option"
    desc: dict[str, Any] = {
        "name": param.name,
        "kind": "option" if is_option else "argument",
        "type": ptype,
        "required": bool(param.required),
        "multiple": bool(getattr(param, "multiple", False)),
        "nargs": int(getattr(param, "nargs", 1) or 1),
        "flag": bool(getattr(param, "is_flag", False)),
        "opts": list(param.opts),
        "secondary_opts": list(getattr(param, "secondary_opts", []) or []),
        "help": _clean(getattr(param, "help", "") or ""),
        "default": None if param.required else _json_safe(param.default),
        **_param_kind(path, str(param.name)),
    }
    if ptype == "choice":
        desc["choices"] = [str(c) for c in param.type.choices]
    for bound in ("min", "max"):
        value = getattr(param.type, bound, None)
        if value is not None:
            desc[bound] = _json_safe(value)
    if getattr(param, "hide_input", False):
        desc["secret"] = True
    desc["blocked"] = param.name in BLOCKED_PARAMS.get(path, frozenset())
    # A command's own `--yes` only skips *its* confirmation prompt, which the console replaces
    # with its own (a click, or the typed command for `destructive`). Always passed, never shown.
    desc["implied"] = param.name == "yes" and desc["flag"]
    if (path, param.name) in _CONSOLE_REQUIRED:
        desc["required"] = True
        desc["default"] = None
    return desc


# Keys every consumer may rely on being present; the rest are omitted when false/empty, which
# roughly halves the catalog a browser downloads (401 commands × ~5 params each).
_CORE_KEYS = frozenset({"name", "kind", "type"})


def _slim(desc: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in desc.items()
        if k in _CORE_KEYS
        or not (v is None or v is False or v == [] or v == "" or v == 1 and k == "nargs")
    }


def _leaves(root: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []

    def walk(cmd: Any, path: list[str]) -> None:
        subs = getattr(cmd, "commands", None)
        if subs:
            for name, sub in sorted(subs.items()):
                walk(sub, [*path, name])
        else:
            found.append((" ".join(path), cmd))

    walk(root, [])
    return found


def live_tree() -> Any:
    """The `exa` Click tree, built exactly as the installed CLI builds it."""
    import typer.main

    from examlops.cli.main import app

    return typer.main.get_command(app)


def root_panels() -> dict[str, str]:
    """Top-level command name → its `exa --help` lifecycle panel title."""
    from examlops.cli.main import _ROOT_PANELS

    return {name: title for title, names in _ROOT_PANELS for name in names}


def leaf_paths() -> list[str]:
    """Every invocable leaf command path in the live CLI tree."""
    return [path for path, _ in _leaves(live_tree())]


def build_catalog() -> dict[str, Any]:
    """Every leaf command as a JSON-safe descriptor, plus a tier summary.

    A command the table does not know is reported as ``admin`` (the safe default) and listed
    under ``unclassified`` — the guard test keeps that list empty in the repo; a plugin command
    installed later lands here instead of silently becoming runnable by viewers.
    """
    panels = root_panels()
    commands: list[dict[str, Any]] = []
    unclassified: list[str] = []
    for path, cmd in _leaves(live_tree()):
        tier = TIERS.get(path)
        if tier is None:
            unclassified.append(path)
            tier = ADMIN
        params = [
            _slim(_describe_param(path, p))
            for p in cmd.params
            if not getattr(p, "hidden", False) and p.name not in ("help",)
        ]
        group = path.split(" ", 1)[0]
        commands.append(
            {
                "path": path,
                "group": group,
                "panel": panels.get(group, "Plugins"),
                "help": _clean(cmd.help),
                "short_help": _clean(cmd.get_short_help_str(limit=120)),
                "examples": _examples(getattr(cmd, "epilog", None)),
                "tier": tier,
                "reason": CLI_ONLY_REASONS.get(path),
                "params": params,
                "forced_args": list(FORCED_ARGS.get(path, ())),
            }
        )
    summary = {t: sum(1 for c in commands if c["tier"] == t) for t in _TIER_ORDER}
    catalog: dict[str, Any] = {
        "commands": commands,
        "total": len(commands),
        "tiers": summary,
        "unclassified": unclassified,
        "panels": [title for title, _ in _root_panel_list()],
    }
    # The same verbs composed into manageable objects (list · create · edit · delete · actions)
    # for the dashboard's Resource Manager. Imported here: `resources` builds on this module.
    from examlops.cli import resources

    catalog["resources"] = resources.build_resources(catalog)
    catalog["resource_coverage"] = resources.coverage(catalog)
    return catalog


def _root_panel_list() -> list[tuple[str, list[str]]]:
    from examlops.cli.main import _ROOT_PANELS

    return list(_ROOT_PANELS)


# ── argv ──────────────────────────────────────────────────────────────────────────────────


@dataclass
class Invocation:
    """A validated command invocation: argv to append after the global options, plus metadata."""

    path: str
    argv: list[str]
    tier: str
    redacted: dict[str, Any] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)
    # ``argv`` with every secret value replaced by ``***`` — safe to show, log and audit.
    display: list[str] = field(default_factory=list)


def contain_path(value: str, root: Path) -> str:
    """Return ``value`` normalised as a path inside ``root``, or raise :class:`SurfaceError`.

    Absolute paths, ``~`` expansion and any ``..`` that climbs out are refused; a symlink inside
    the workspace pointing elsewhere is refused too (the check is on the resolved path). Returns
    the normalised workspace-relative form; :func:`build_argv` hands the command the absolute path.
    """
    if not value or "\x00" in value:
        raise SurfaceError("empty or invalid path")
    if value.startswith("~") or os.path.isabs(value) or re.match(r"^[A-Za-z]:[\\/]", value):
        raise SurfaceError(f"path must be relative to the CLI workspace: {value!r}")
    norm = os.path.normpath(value)
    if norm == ".." or norm.startswith(".." + os.sep):
        raise SurfaceError(f"path escapes the CLI workspace: {value!r}")
    base = root.resolve()
    target = (base / norm).resolve()
    if target != base and base not in target.parents:
        raise SurfaceError(f"path escapes the CLI workspace: {value!r}")
    return norm


def _escalate(tier: str, to: str) -> str:
    return to if _TIER_ORDER[to] > _TIER_ORDER[tier] else tier


def _as_text(name: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SurfaceError(f"--{name}: expected text")
    text = str(value)
    if "\x00" in text or len(text) > _MAX_VALUE_LEN:
        raise SurfaceError(f"--{name}: value is too long or contains a NUL byte")
    return text


def _coerce(spec: dict[str, Any], value: Any) -> str:
    name = spec["name"]
    ptype = spec["type"]
    if ptype == "int":
        if isinstance(value, bool):
            raise SurfaceError(f"{name}: expected an integer")
        try:
            number: int | float = int(str(value).strip())
        except ValueError as exc:
            raise SurfaceError(f"{name}: expected an integer") from exc
    elif ptype == "float":
        if isinstance(value, bool):
            raise SurfaceError(f"{name}: expected a number")
        try:
            number = float(str(value).strip())
        except ValueError as exc:
            raise SurfaceError(f"{name}: expected a number") from exc
        if not math.isfinite(number):
            raise SurfaceError(f"{name}: expected a finite number")
    else:
        text = _as_text(name, value)
        if ptype == "choice" and text not in spec.get("choices", []):
            raise SurfaceError(f"{name}: must be one of {', '.join(spec['choices'])}")
        return text
    if spec.get("min") is not None and number < spec["min"]:
        raise SurfaceError(f"{name}: must be ≥ {spec['min']}")
    if spec.get("max") is not None and number > spec["max"]:
        raise SurfaceError(f"{name}: must be ≤ {spec['max']}")
    return str(number)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, list) and not value)


def build_argv(command: dict[str, Any], values: dict[str, Any], *, workspace: Path) -> Invocation:
    """Validate ``values`` against ``command`` (a :func:`build_catalog` descriptor) → argv.

    Options are emitted as ``--long=value`` (a value can never be read as another flag) and
    positionals come after ``--`` (a value starting with ``-`` stays a value). The returned
    ``tier`` is the command's tier raised by what was supplied: a persisting flag, a filesystem
    path or a network target turns a ``read`` into ``admin``. Filesystem paths reach the command
    as absolute paths inside ``workspace``; ``display``/``redacted``/``paths`` keep the
    workspace-relative form a person typed.
    """
    path = command["path"]
    tier = command["tier"]
    if tier == CLI_ONLY:
        raise SurfaceError(command.get("reason") or f"`exa {path}` cannot run from the dashboard")
    specs = {p["name"]: p for p in command["params"]}
    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise SurfaceError(f"unknown parameter(s) for `exa {path}`: {', '.join(unknown)}")

    options: list[str] = []
    positionals: list[str] = []
    shown_options: list[str] = []
    shown_positionals: list[str] = []
    redacted: dict[str, Any] = {}
    contained: list[str] = []

    for spec in command["params"]:
        name = spec["name"]
        value = values.get(name)
        is_flag = bool(spec.get("flag"))
        secondary = spec.get("secondary_opts") or []
        if spec.get("implied"):
            options.append(_long(spec["opts"]))
            shown_options.append(options[-1])
            continue
        if is_flag and spec.get("persisting"):
            # A persisting flag counts at its *effective* value: `cards model` saves unless
            # told `--no-save`, so leaving the box untouched is itself a write.
            effective = value if isinstance(value, bool) else bool(spec.get("default"))
            if effective:
                tier = _escalate(tier, ADMIN)
        if _is_empty(value) or (is_flag and value is False and not secondary):
            if spec.get("required"):
                raise SurfaceError(f"missing required parameter: {name}")
            continue
        if spec.get("blocked"):
            raise SurfaceError(f"--{name} is not available from the dashboard for `exa {path}`")

        items = value if isinstance(value, list) else [value]
        if len(items) > 1 and not (spec.get("multiple") or spec.get("nargs", 1) != 1):
            raise SurfaceError(f"{name}: takes a single value")

        if is_flag:
            if not isinstance(value, bool):
                raise SurfaceError(f"{name}: expected true or false")
            if value == bool(spec.get("default")):
                continue  # already the command's default — nothing to say
            options.append(_long(spec["opts"] if value else secondary))
            shown_options.append(options[-1])
            redacted[name] = value
            continue

        rendered: list[str] = []  # what the subprocess receives
        shown: list[str] = []  # what a person sees (workspace-relative paths, secrets masked)
        for item in items:
            text = _coerce(spec, item)
            as_typed = text
            if spec.get("path"):
                if not (spec.get("path_or_url") and re.match(r"^https?://", text)):
                    as_typed = contain_path(text, workspace)
                    contained.append(as_typed)
                    # Absolute, so the command may run from any directory (the repo root, as an
                    # operator would) and still read/write exactly this workspace file.
                    text = str(workspace.resolve() / as_typed)
                tier = _escalate(tier, ADMIN)
            rendered.append(text)
            shown.append("***" if spec.get("secret") else as_typed)
        if spec.get("network") or spec.get("persisting"):
            tier = _escalate(tier, ADMIN)

        redacted[name] = shown if len(shown) > 1 else shown[0]
        masked = shown
        if spec["kind"] == "argument":
            positionals.extend(rendered)
            shown_positionals.extend(masked)
        else:
            opt = _long(spec["opts"])
            # `--name=value` / `-nvalue`: the value is glued to its option, so a value that
            # starts with `-` can never be parsed as a separate flag.
            glue = "=" if opt.startswith("--") else ""
            options.extend(f"{opt}{glue}{text}" for text in rendered)
            shown_options.extend(f"{opt}{glue}{text}" for text in masked)

    forced = list(command.get("forced_args", []))
    argv = [*path.split(" "), *options, *forced]
    display = [*path.split(" "), *shown_options, *forced]
    if positionals:
        argv += ["--", *positionals]
        display += ["--", *shown_positionals]
    return Invocation(
        path=path, argv=argv, tier=tier, redacted=redacted, paths=contained, display=display
    )


def _long(opts: list[str]) -> str:
    """Prefer the ``--long`` spelling, which is the one ``--name=value`` works with."""
    for opt in opts:
        if opt.startswith("--"):
            return opt
    return opts[0]


def valid_context(name: str) -> str:
    """A config-context name safe to pass as ``--context`` (letters, digits, ``_.-``)."""
    if not _CONTEXT_RE.match(name or ""):
        raise SurfaceError("context must be 1–64 characters of letters, digits, '_', '.' or '-'")
    return name


def tier_rank(tier: str) -> int:
    return _TIER_ORDER[tier]


if __name__ == "__main__":  # `python -m examlops.cli.surface` → the catalog as JSON
    print(json.dumps(build_catalog()))
