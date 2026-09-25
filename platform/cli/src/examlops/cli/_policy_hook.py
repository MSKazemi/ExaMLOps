"""Every mutating ``exa`` command consults ``policy.decide`` — by construction (ADR 0079 d2).

ADR 0079 decision 2 says *every* mutation consults the policy decision point before acting. The
CLI met that one command at a time (``exa retrain``, manual promote, cluster approval, …), so a
new mutating command was ungoverned until someone remembered to call the gate, and most were
(``exa project grant``, ``exa secrets set``, ``exa serve traffic``, …). The dashboard and the
control plane closed the same gap with a route table and a guard; this module is the CLI's
equivalent, and it is stronger: the default for a command is **gated**, so a new command is
governed the moment it exists.

How it works
------------
The root group (:class:`examlops.cli._modules_gate.ModuleGatedGroup`) calls :func:`install` on
every root command it resolves. :func:`install` walks that command's subtree and wraps each leaf
callback once. When the leaf runs, :func:`classify` decides — from the command's surface tier
(:data:`examlops.cli.surface.TIERS`) and the two tables below — whether it is:

* ``gated`` — ``admin`` / ``destructive`` / ``cli_only`` commands (and third-party plugin
  commands, which have no tier row) consult ``policy.decide_safe(action, context)`` *before* the
  command body runs. ``action`` is :data:`ACTION_NAMES`' entry, else the command path with
  spaces and hyphens as underscores (``exa project grant`` → ``project_grant``).
* ``self`` — the command body already consults policy under its own established action name
  (:data:`SELF_GATED`); the hook stays out of the way so one rule is not evaluated twice.
* ``exempt`` — a sensitive *read*, or a change to this operator's own client configuration or
  session (:data:`EXEMPT`, each with its reason).
* ``read`` — ``read``-tier commands are not mutations.

Contract — identical to :mod:`examlops.cli._policy_gate` (the per-command gate):

* No policy file / no matching rule → the command runs exactly as before, **no audit row**.
* A rule matched (an explicit ``allow`` included) → audited as ``policy:<action>``;
  ``mode: monitor`` rules are audited as ``policy_monitor:<action>`` and never block.
* ``deny`` → an error naming the rule, exit 1, the command body never runs.
* ``require_approval`` → a default-*no* confirmation. A human may approve (or pass the global
  ``--yes``; ``-o json`` alone is an output format, not consent, and is refused); an agent
  principal is refused (``plan_required``). A declined approval exits 1 and
  changes nothing. An approved one runs the body inside
  :func:`examlops.policy.http_gate.approval_acknowledged`, so a control plane that re-evaluates
  the same action (``approval_approve``, ``production_reload``, …) sees the human's approval.
* An engine bug → deny, and ``decide_safe`` audits that policy was unavailable (fail closed).
* ``--dry-run`` → the gate is skipped: a preview changes nothing, and refusing to show what
  *would* happen would hide the very thing a reviewer needs to see.

``tests/unit/test_policy_cli_hook.py`` walks the live tree and fails when a table entry names a
command that no longer exists, when a ``self`` entry's module does not actually call the policy
layer, or when a derived action name collides with another command's.
"""

from __future__ import annotations

import contextlib
import functools
import re
from typing import Any

_HOOKED_ATTR = "_examlops_policy_hooked"

# Commands whose body consults the policy layer itself, and the action name it uses.
SELF_GATED: dict[str, str] = {
    "agent alias rollback": "agent_promote",
    "agent alias set": "agent_promote",
    "autopilot follow": "autopilot_trigger",
    "autopilot run": "autopilot_trigger",
    "broker grant remove": "tool_grant_change",
    "broker grant set": "tool_grant_change",
    "genai-app promote": "genai_app_promote",
    "hpc approve": "cluster_approve",
    "hpc connect": "connect_cluster",
    "hpc reject": "cluster_reject",
    "models sign": "model_sign",
    "pipeline compile": "pipeline_compile",
    "pipeline promote": "manual_promote",
    "project archive": "project_archive",
    "project delete": "project_delete",
    "project remove-member": "project_remove_member",
    "retrain": "retrain",
    "secrets rotate": "secret_rotate",
}

# Explicit action names where a vocabulary already exists at another door, so one rule governs
# both (the dashboard's and the control plane's approval routes use these names).
ACTION_NAMES: dict[str, str] = {
    "approvals approve": "approval_approve",
    "approvals reject": "approval_reject",
}

_READ = (
    "Sensitive read: admin tier decides who may see it, and it changes no platform state, so it "
    "is not a mutation for policy.decide."
)
_CLIENT = (
    "Changes only this operator's local client configuration (config.toml / active context), "
    "never platform state."
)
_SESSION = "Authentication / session lifecycle of the operator themself: no platform resource."
_AGENT_SURFACE = (
    "Hosts an agent surface; every write the agent makes is its own tool call, gated per call "
    "as `agent_write` (ADR 0079 d3) — gating the host process would not add a decision."
)

EXEMPT: dict[str, str] = {
    "agent memory export": _READ,
    "agent memory list": _READ,
    "agent memory review list": _READ,
    "approvals list": _READ,
    "audit chain": _READ,
    "audit checkpoints": _READ,
    "audit export": _READ,
    "audit reviews": _READ,
    "audit verify": _READ,
    "audit verify-anchors": _READ,
    "audit verify-worm": _READ,
    "auth accounts": _READ,
    "auth decide": _READ,
    "auth validate": _READ,
    "auth verify": _READ,
    "auth login": _SESSION,
    "auth logout": _SESSION,
    "auth token": _SESSION,
    "backup list": _READ,
    "backup status": _READ,
    "backup verify": _READ,
    "backup verify-bundle": _READ,
    "chat": _AGENT_SURFACE,
    "compliance status": _READ,
    "config delete-context": _CLIENT,
    "config export": _READ,
    "config init": _CLIENT,
    "config set": _CLIENT,
    "config unset": _CLIENT,
    "config use": _CLIENT,
    "exchange inspect": _READ,
    "exchange verify": _READ,
    "fairness show": _READ,
    "gateway key list": _READ,
    "hpc detect": _READ,
    "hpc gpus": _READ,
    "hpc nodes": _READ,
    "mcp serve": _AGENT_SURFACE,
    "models verify": _READ,
    "plan list": _READ,
    "plan show": _READ,
    "policy eval": "A dry-run evaluation of policy itself: it decides nothing and changes nothing.",
    "project use": _CLIENT,
    "secrets list": _READ,
}

_GATED_TIERS = frozenset({"admin", "destructive", "cli_only"})

# Parameter values that never enter a decision context (they would land in the audit log).
_SENSITIVE = re.compile(
    r"secret|password|passwd|token|api[_-]?key|credential|private|value|content|body", re.I
)
# A parameter whose *help* opens by naming a credential is secret whatever it is called
# (`exa gateway chat --key` is "Virtual key to authenticate with"; the name alone reads like an
# idempotency key). Anchored at the start so help that merely *mentions* credentials ("Named
# Connection holding the credentials", "Tenant whose virtual keys …") keeps its vocabulary key.
_SECRET_HELP = re.compile(
    r"^\s*(?:the |a |an |your )?(?:virtual[ _-]?key|api[ _-]?key|password|passphrase|"
    r"secret(?: value)?\b(?! path| name| id| ref)|"
    r"(?:bearer|access|refresh|auth|session)[ _-]?token|credential|"
    r"private[ _-]?key\b(?! path| file))",
    re.I,
)


def secret_params(cmd: Any) -> frozenset[str]:
    """Names of ``cmd``'s parameters whose value must never enter a decision context."""
    names: set[str] = set()
    for param in getattr(cmd, "params", None) or []:
        name = getattr(param, "name", None)
        if not name:
            continue
        if (
            _SENSITIVE.search(name)
            or getattr(param, "hide_input", False)
            or _SECRET_HELP.search(str(getattr(param, "help", "") or ""))
        ):
            names.add(name)
    return frozenset(names)


# Parameter names that carry the CLI's shared vocabulary (the keys `policy.yaml` conditions read).
_VOCAB = {
    "model": "model",
    "model_id": "model",
    "model_name": "model",
    "project": "project",
    "cluster": "cluster",
    "dataset": "dataset",
    "version": "version",
    "tenant": "tenant",
}
_MAX_KEYS = 32
_MAX_LEN = 256


def action_for(path: str) -> str:
    """The policy action name for command ``path`` (``"project grant"`` → ``project_grant``)."""
    if path in SELF_GATED:
        return SELF_GATED[path]
    return ACTION_NAMES.get(path) or re.sub(r"[\s\-]+", "_", path.strip())


def classify(path: str, tier: str | None) -> tuple[str, str]:
    """``(kind, detail)``: ``gated``/``self`` → action, ``exempt`` → reason, ``read`` → tier."""
    if path in SELF_GATED:
        return "self", SELF_GATED[path]
    if path in EXEMPT:
        return "exempt", EXEMPT[path]
    if tier is None or tier in _GATED_TIERS:  # no tier row = a third-party plugin: govern it
        return "gated", action_for(path)
    return "read", tier


def _scalar(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:_MAX_LEN]
    if hasattr(value, "value") and isinstance(value.value, str):  # an Enum choice
        return value.value[:_MAX_LEN]
    return None


def build_context(
    path: str, tier: str | None, params: dict[str, Any], secret: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """The decision input for one invocation — bounded, and free of secret-bearing values.

    ``secret`` names parameters declared secret by their metadata (:func:`secret_params`), on
    top of the name pattern every parameter is checked against.
    """
    from examlops.cli import _output
    from examlops.platform_db import _actor

    ctx: dict[str, Any] = {}
    for name, raw in params.items():
        if len(ctx) >= _MAX_KEYS:
            break
        if raw is None or name in secret or _SENSITIVE.search(name):
            continue
        value = _scalar(raw)
        if value is None:
            continue
        ctx[name] = value
        vocab = _VOCAB.get(name)
        if vocab and vocab not in ctx:
            ctx[vocab] = value
    first = next((v for v in ctx.values() if isinstance(v, str)), None)
    ctx.setdefault("target", first or path)
    # Identity keys last, so a parameter can never impersonate the caller or the command.
    ctx.update(
        {
            "command": path,
            "tier": tier or "plugin",
            "actor": _actor(),
            "principal_kind": _output.principal_kind(),
            "via": "cli",
        }
    )
    return ctx


def enforce(
    path: str, tier: str | None, params: dict[str, Any], secret: frozenset[str] = frozenset()
) -> contextlib.AbstractContextManager:
    """Consult policy for one gated invocation; exit on deny/decline, else a context to run in."""
    from examlops import policy
    from examlops.cli import _output
    from examlops.policy import http_gate

    action = action_for(path)
    context = build_context(path, tier, params, secret)
    decision = policy.decide_safe(action, context, default_effect=policy.DENY, audit=False)
    if decision.rule is not None or decision.shadow:
        policy.record_decision(action, context, decision)
    if decision.denied:
        _output.error(
            f"Policy denied `exa {path}` ({action}): {decision.reason}",
            hint="See your policy.yaml or run: exa policy list",
        )
    if decision.requires_approval:
        # `_output.confirm` treats structured output (`-o json`) as "don't prompt, say yes" for
        # a human's script. For a policy rule whose whole point is a human's approval that is
        # consent nobody gave: an output format is not an approval. Only an explicit global
        # `--yes` (or an answered prompt) approves; an agent is refused inside `confirm`.
        if _output.json_mode and not _output.yes_mode and _output.principal_kind() != "agent":
            _output.error(
                f"Not approved — `exa {path}` did not run and nothing was changed.",
                hint=f"Policy rule {decision.rule!r} requires approval for {action}; structured "
                "output is not consent — re-run with the global --yes to approve.",
            )
        if not _output.confirm(
            f"Run `exa {path}`? [policy rule {decision.rule!r} requires approval]", default=False
        ):
            _output.error(
                f"Not approved — `exa {path}` did not run and nothing was changed.",
                hint=f"Policy rule {decision.rule!r} requires a human's approval for {action}.",
            )
        # The approval itself is a decision (ADR 0079 d5) — recorded as the HTTP doors record it
        # (`policy_approval:<action>`, approved_by), including *how* consent was given.
        from examlops.data.audit import audit_best_effort

        audit_best_effort(
            "exa-policy",
            str(context["actor"]),
            f"policy_approval:{action}",
            str(context.get("model") or context.get("target") or ""),
            {
                "rule": decision.rule,
                "approved_by": context["actor"],
                "via": "cli",
                "consent": "--yes" if _output.yes_mode else "prompt",
            },
        )
        return http_gate.approval_acknowledged()
    return contextlib.nullcontext()


def _wrap(cmd: Any, path: str) -> None:
    if getattr(cmd, _HOOKED_ATTR, False) or cmd.callback is None:
        return
    original = cmd.callback
    secret = secret_params(cmd)

    @functools.wraps(original)
    def gated(*args: Any, **kwargs: Any) -> Any:
        from examlops.cli.surface import TIERS

        tier = TIERS.get(path)
        kind, _ = classify(path, tier)
        if kind != "gated" or kwargs.get("dry_run") is True:
            return original(*args, **kwargs)
        with enforce(path, tier, kwargs, secret):
            return original(*args, **kwargs)

    cmd.callback = gated
    setattr(cmd, _HOOKED_ATTR, True)


def install(cmd: Any, name: str) -> Any:
    """Wrap every leaf under root command ``name`` (idempotent); return ``cmd`` unchanged."""

    def walk(node: Any, parts: list[str]) -> None:
        subs = getattr(node, "commands", None)
        if subs:
            for sub_name, sub in subs.items():
                walk(sub, [*parts, sub_name])
        elif node is not None:
            _wrap(node, " ".join(parts))

    # Deliberately not wrapped in a try: a tree the hook cannot walk must fail loudly rather
    # than leave its commands silently ungoverned.
    walk(cmd, [name])
    return cmd
