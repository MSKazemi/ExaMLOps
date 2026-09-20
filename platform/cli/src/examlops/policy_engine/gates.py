"""Opt-in wiring of the engine's built-in domain gates into real decision points (ADR 0029 d3/d4).

``supply_chain_gate``, ``budget_gate`` and ``card_gate`` used to be reachable only from
``exa policy eval`` and tests. This module lets a site *arm* them at the decision points that
matter — verify-before-load and manual promote (supply chain, model card) and the training entry
point (budget) — with a per-gate rollout mode:

* ``off`` (the default) — the gate is never consulted: no check, no audit row, behaviour
  byte-identical to before the gate was wired.
* ``monitor`` — the gate is evaluated and audited (``policy_gate_monitor:<gate>``); a deny is
  recorded as *would-deny* and the caller proceeds.
* ``enforce`` — a deny blocks the caller.

Configuration, highest precedence first: ``EXAMLOPS_POLICY_GATES`` (``supply_chain=enforce,
budget=monitor,model_card=enforce``), then a top-level ``gates:`` mapping in ``policy.yaml``::

    gates:
      supply_chain: enforce
      budget: monitor
      model_card: {mode: enforce, floor: 0.9}

An unrecognized mode fails **closed** to ``enforce``: a typo must not turn a control off.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from . import EngineDecision, PolicyInput, _audit

log = logging.getLogger("examlops.policy.gates")

GATE_NAMES = ("supply_chain", "budget", "model_card")
OFF, MONITOR, ENFORCE = "off", "monitor", "enforce"
_MODES = {OFF, MONITOR, ENFORCE}
ENV_VAR = "EXAMLOPS_POLICY_GATES"


def _coerce_mode(raw: Any, *, gate: str) -> str:
    mode = str(raw).strip().lower()
    if mode in _MODES:
        return mode
    log.warning("unknown gate mode %r for %r — failing closed to 'enforce'", raw, gate)
    return ENFORCE


def _from_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for part in os.getenv(ENV_VAR, "").split(","):
        if "=" in part:
            name, _, mode = part.partition("=")
            if name.strip():
                out[name.strip()] = mode
    return out


def _from_file() -> dict[str, Any]:
    from examlops import policy

    path = policy.POLICY_YAML
    try:
        if not path.is_file():
            return {}
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
    except Exception:  # noqa: BLE001 - an unreadable file leaves every gate off (status shown by list)
        return {}
    gates = data.get("gates") if isinstance(data, dict) else None
    return dict(gates) if isinstance(gates, dict) else {}


def configured_gates() -> dict[str, dict[str, Any]]:
    """``{gate: {"mode": off|monitor|enforce, ...options}}`` for every known gate."""
    file_cfg = _from_file()
    env_cfg = _from_env()
    result: dict[str, dict[str, Any]] = {}
    for name in GATE_NAMES:
        entry = file_cfg.get(name)
        opts: dict[str, Any] = {}
        if isinstance(entry, dict):
            opts = {k: v for k, v in entry.items() if k != "mode"}
            raw: Any = entry.get("mode", ENFORCE)
        elif entry is not None:
            raw = entry
        else:
            raw = OFF
        if name in env_cfg:
            raw = env_cfg[name]
        result[name] = {"mode": _coerce_mode(raw, gate=name), **opts}
    return result


def gate_mode(name: str) -> str:
    return configured_gates().get(name, {}).get("mode", OFF)


def gate_options(name: str) -> dict[str, Any]:
    return {k: v for k, v in configured_gates().get(name, {}).items() if k != "mode"}


def consult(
    gate: str, evaluator: Callable[[dict[str, Any]], EngineDecision]
) -> EngineDecision | None:
    """Run ``evaluator(options)`` when ``gate`` is armed; ``None`` when it is off.

    In ``monitor`` mode a denial is audited as would-deny and converted to an allow, so the
    caller never blocks. In ``enforce`` mode the engine's decision is returned as is.
    """
    cfg = configured_gates().get(gate, {"mode": OFF})
    mode = cfg["mode"]
    if mode == OFF:
        return None
    opts = {k: v for k, v in cfg.items() if k != "mode"}
    result = evaluator(opts)
    if mode == MONITOR and not result.allow:
        _audit(
            f"gate_monitor:{gate}",
            PolicyInput(action=gate),
            EngineDecision(
                True,
                [f"monitor: would {result.effect} — " + "; ".join(result.reasons)],
                "allow",
                result.engine,
            ),
        )
        return EngineDecision(
            True,
            [f"monitor: {gate} would {result.effect} — " + "; ".join(result.reasons)],
            "allow",
            result.engine,
        )
    return result
