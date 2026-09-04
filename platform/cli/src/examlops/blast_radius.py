"""Blast-radius contracts and per-behaviour autonomy (ADR 0113 decisions 1, 3, 4).

Every autonomous behaviour publishes a **machine-readable, versioned contract** stating what it
may and may not change, how far one action may reach, and how it is undone. The contract is
*enforced* at the decision point — a change outside ``may_change`` is denied and the denial names
the clause — so contract drift surfaces as a failure, never as a stale document.

Autonomy is per behaviour, not global (decision 3): ``AUTONOMOUS | REVIEW | DISABLED``, each
individually pausable without losing configuration, and granting AUTONOMOUS records an explicit
human acknowledgment. State lives in the existing ``autopilot_config`` key/value store
(``autonomy:<behaviour>``), so no schema change is needed and pausing preserves everything else.

The live-run interrupt (decision 4) uses the same store: ``interrupt:<run_id>`` ∈
``freeze | kill`` flags one in-flight cycle, and ``quarantine:<model>`` excludes one model from
autonomous action until released. The autopilot polls these at its checkpoints.

Contracts are Python data with an optional YAML overlay (``EXAMLOPS_CONTRACTS_FILE``) so an
operator can tighten a bound without a deploy; the overlay may only *narrow* autonomy defaults,
never widen extents beyond the built-ins (widening requires a code change and review).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

AUTONOMOUS = "AUTONOMOUS"
REVIEW = "REVIEW"
DISABLED = "DISABLED"
_LEVELS = (AUTONOMOUS, REVIEW, DISABLED)


@dataclass(frozen=True)
class Contract:
    """One behaviour's declarative blast-radius contract (ADR 0113 decision 1)."""

    behaviour: str
    version: int
    default_autonomy: str
    may_change: tuple[str, ...]
    may_not_change: tuple[str, ...]
    max_extent_per_action: dict[str, float] = field(default_factory=dict)
    requires_rollback: bool = True
    rollback: str = ""
    cooldown_s: int = 3600
    kill_switch: str = "EXAMLOPS_AUTOPILOT_ENABLED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "behaviour": self.behaviour,
            "version": self.version,
            "autonomy": self.default_autonomy,
            "may_change": list(self.may_change),
            "may_not_change": list(self.may_not_change),
            "max_extent_per_action": dict(self.max_extent_per_action),
            "requires_rollback": self.requires_rollback,
            "rollback": self.rollback,
            "cooldown_s": self.cooldown_s,
            "kill_switch": self.kill_switch,
        }


# The built-in contracts describe what the autopilot ACTUALLY does today — publishing them is
# the point (decision 1); tightening them is an operator overlay away.
DEFAULT_CONTRACTS: dict[str, Contract] = {
    "drift_auto_retrain": Contract(
        behaviour="drift_auto_retrain",
        version=1,
        default_autonomy=AUTONOMOUS,
        may_change=("pipeline_run", "model_version"),
        may_not_change=(
            "model_alias:Production",
            "project_quota",
            "connections",
            "secrets",
            "hpc_clusters",
        ),
        max_extent_per_action={"models": 1, "runs": 1},
        requires_rollback=True,
        rollback="exa models rollback run {model} --version {previous}",
        cooldown_s=3600,
    ),
    "autopilot_promote": Contract(
        behaviour="autopilot_promote",
        version=1,
        default_autonomy=AUTONOMOUS,
        may_change=("model_alias:Production", "model_alias:Canary", "model_alias:Staging"),
        may_not_change=("project_quota", "connections", "secrets", "hpc_clusters"),
        max_extent_per_action={"models": 1, "aliases": 1},
        requires_rollback=True,
        rollback="exa models rollback alias {model} {to_alias} --version {previous}",
        cooldown_s=0,
    ),
}


def load_contracts() -> dict[str, Contract]:
    """Built-in contracts, with an optional YAML overlay that may only narrow them.

    Overlay path: ``EXAMLOPS_CONTRACTS_FILE``. Recognised overlay keys per behaviour:
    ``autonomy`` (may only move toward REVIEW/DISABLED), ``may_not_change`` (additions only) and
    ``max_extent_per_action`` (each bound may only shrink). Anything else is ignored — widening
    a contract is a code change, not a config edit.
    """
    contracts = dict(DEFAULT_CONTRACTS)
    path = os.getenv("EXAMLOPS_CONTRACTS_FILE", "").strip()
    if not path or not os.path.exists(path):
        return contracts
    try:
        import yaml

        with open(path) as fh:
            overlay = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001 — a broken overlay must not disable enforcement
        return contracts
    if not isinstance(overlay, dict):
        return contracts
    rank = {AUTONOMOUS: 0, REVIEW: 1, DISABLED: 2}
    for name, spec in overlay.items():
        base = contracts.get(name)
        if base is None or not isinstance(spec, dict):
            continue
        autonomy = str(spec.get("autonomy", base.default_autonomy)).upper()
        if autonomy not in _LEVELS or rank[autonomy] < rank[base.default_autonomy]:
            autonomy = base.default_autonomy
        extra_forbidden = tuple(
            str(t) for t in spec.get("may_not_change", []) if isinstance(t, str)
        )
        extents = dict(base.max_extent_per_action)
        for k, v in (spec.get("max_extent_per_action") or {}).items():
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if k in extents and v < extents[k]:
                extents[k] = v
        contracts[name] = Contract(
            behaviour=base.behaviour,
            version=base.version,
            default_autonomy=autonomy,
            may_change=base.may_change,
            may_not_change=tuple(dict.fromkeys((*base.may_not_change, *extra_forbidden))),
            max_extent_per_action=extents,
            requires_rollback=base.requires_rollback,
            rollback=base.rollback,
            cooldown_s=base.cooldown_s,
            kill_switch=base.kill_switch,
        )
    return contracts


def _token_matches(token: str, listed: tuple[str, ...]) -> bool:
    """Exact token match, or a ``kind:*`` wildcard entry matching any ``kind:<qualifier>``."""
    if token in listed:
        return True
    kind = token.split(":", 1)[0]
    return f"{kind}:*" in listed


def check_change(
    behaviour: str, change: str, extent: dict[str, float] | None = None
) -> tuple[bool, str]:
    """Is ``change`` (a ``kind`` or ``kind:qualifier`` token) within the behaviour's contract?

    Returns ``(True, "")`` or ``(False, clause)`` where ``clause`` names the exact contract
    clause that denied it (decision 1: "the denial names the contract clause").
    """
    contract = load_contracts().get(behaviour)
    if contract is None:
        return False, f"no blast-radius contract published for behaviour {behaviour!r}"
    if _token_matches(change, contract.may_not_change):
        return (
            False,
            f"{behaviour} contract v{contract.version}: may_not_change includes {change!r}",
        )
    if not _token_matches(change, contract.may_change):
        return False, (
            f"{behaviour} contract v{contract.version}: {change!r} is not in may_change "
            f"{list(contract.may_change)}"
        )
    for key, asked in (extent or {}).items():
        cap = contract.max_extent_per_action.get(key)
        if cap is not None and asked > cap:
            return False, (
                f"{behaviour} contract v{contract.version}: max_extent_per_action.{key} "
                f"is {cap}, action asks {asked}"
            )
    return True, ""


# ── per-behaviour autonomy state (decision 3) ─────────────────────────────────


def get_autonomy(behaviour: str) -> str:
    """Effective autonomy level: operator-set state, else the contract's default.

    A behaviour set AUTONOMOUS without a recorded acknowledgment is treated as REVIEW —
    the grant is not effective until a human has acknowledged it (decision 3).
    """
    from examlops.data.autopilot import get_autopilot_config

    contract = load_contracts().get(behaviour)
    default = contract.default_autonomy if contract else REVIEW
    level = (get_autopilot_config(f"autonomy:{behaviour}") or default).upper()
    if level not in _LEVELS:
        level = default
    if level == AUTONOMOUS:
        # The built-in default AUTONOMOUS is itself covered by the global kill-switch being an
        # explicit enable; an OPERATOR-set AUTONOMOUS needs its recorded acknowledgment.
        stored = get_autopilot_config(f"autonomy:{behaviour}")
        if stored is not None and get_autopilot_config(f"autonomy_ack:{behaviour}") is None:
            return REVIEW
    return level


def set_autonomy(behaviour: str, level: str, *, actor: str, acknowledgment: str = "") -> None:
    """Set a behaviour's autonomy level; AUTONOMOUS requires a human acknowledgment.

    Pausing (REVIEW/DISABLED) never touches the rest of the behaviour's configuration —
    that is the "individually pausable without losing config" property.
    """
    from examlops.data.audit import write_audit_event
    from examlops.data.autopilot import set_autopilot_config

    level = level.upper()
    if level not in _LEVELS:
        raise ValueError(f"autonomy must be one of {_LEVELS}, got {level!r}")
    if level == AUTONOMOUS and not acknowledgment.strip():
        raise ValueError(
            'granting AUTONOMOUS requires an explicit acknowledgment (--ack "<text>") — '
            "the grant is recorded with who acknowledged it and why (ADR 0113 decision 3)"
        )
    set_autopilot_config(f"autonomy:{behaviour}", level)
    if level == AUTONOMOUS:
        set_autopilot_config(
            f"autonomy_ack:{behaviour}",
            json.dumps({"by": actor, "ack": acknowledgment.strip()}),
        )
    write_audit_event(
        "cli",
        actor,
        "autonomy_changed",
        behaviour,
        {"level": level, "acknowledgment": acknowledgment.strip() or None},
    )


# ── live-run interrupt + model quarantine (decision 4) ────────────────────────


def request_interrupt(run_id: int, action: str, *, actor: str, reason: str = "") -> None:
    """Flag one in-flight autopilot run: ``freeze`` (pause at next checkpoint) or ``kill``."""
    from examlops.data.audit import write_audit_event
    from examlops.data.autopilot import set_autopilot_config

    if action not in ("freeze", "kill"):
        raise ValueError("interrupt action must be 'freeze' or 'kill'")
    set_autopilot_config(f"interrupt:{run_id}", action)
    write_audit_event(
        "cli", actor, f"run_{action}_requested", str(run_id), {"reason": reason or None}
    )


def clear_interrupt(run_id: int, *, actor: str) -> None:
    from examlops.data.audit import write_audit_event
    from examlops.data.autopilot import set_autopilot_config

    set_autopilot_config(f"interrupt:{run_id}", "")
    write_audit_event("cli", actor, "run_resumed", str(run_id), None)


def pending_interrupt(run_id: int) -> str | None:
    """The pending interrupt action for a run, or None. Polled at cycle checkpoints."""
    from examlops.data.autopilot import get_autopilot_config

    val = (get_autopilot_config(f"interrupt:{run_id}") or "").strip()
    return val or None


def quarantine_model(model: str, *, actor: str, reason: str = "") -> None:
    """Exclude one model from autonomous action until released (audited)."""
    from examlops.data.audit import write_audit_event
    from examlops.data.autopilot import set_autopilot_config

    set_autopilot_config(f"quarantine:{model}", reason.strip() or "quarantined")
    write_audit_event("cli", actor, "model_quarantined", model, {"reason": reason or None})


def release_model(model: str, *, actor: str) -> None:
    from examlops.data.audit import write_audit_event
    from examlops.data.autopilot import set_autopilot_config

    set_autopilot_config(f"quarantine:{model}", "")
    write_audit_event("cli", actor, "model_released", model, None)


def quarantine_reason(model: str) -> str | None:
    from examlops.data.autopilot import get_autopilot_config

    val = (get_autopilot_config(f"quarantine:{model}") or "").strip()
    return val or None
