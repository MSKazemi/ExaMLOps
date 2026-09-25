"""Register, alias, promote and resolve agent versions (ADR 0146).

The promotion gate reuses the platform's own: ``exa eval gate`` config and results, keyed by the
registered-model name ``agent-<name>`` (the ADR's naming), with ``model_version`` = the agent's
``version_id``. So the judge-calibration rule of ADR 0111 (``no uncalibrated judge may gate``) is
the same code path a predictive model takes, not a copy of it.

**Absence is not evidence.** No configured gate, a gate in ``warn`` mode, a metric with no score
for this version, a declared suite with no results - each refuses, with the reason named. Rolling
back is different: it restores a version that already passed this gate, so it is not re-gated.
"""

from __future__ import annotations

import base64
import hmac
import os
from dataclasses import dataclass
from typing import Any

from examlops.agent_versions.manifest import (
    ALIASES,
    AgentManifestError,
    canonical_json,
    diff_manifests,
    normalize,
    version_id_of,
)
from examlops.data import agent_versions as store
from examlops.data.audit import audit_best_effort

__all__ = [
    "AgentVersion",
    "GateRefusal",
    "canonical_alias",
    "diff",
    "evidence_refusals",
    "get",
    "list_versions",
    "register",
    "resolve",
    "rollback",
    "set_alias",
    "verify_signature",
]

_SOURCE = "agent-versions"
_STATEMENT = "examlops-agent-version-v1"


class GateRefusal(RuntimeError):
    """A promotion was refused; ``reasons`` names each unmet condition."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class AgentVersion:
    version_id: str
    agent: str
    manifest: dict[str, Any]
    signed: bool = False

    def prompt(self, name: str) -> int | None:
        """The pinned version of prompt ``name``, or None when this agent does not use it."""
        for p in self.manifest["prompts"]:
            if p["name"] == name:
                return int(p["version"])
        return None


def _actor(actor: str | None) -> str:
    return actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"


def canonical_alias(alias: str) -> str:
    for a in ALIASES:
        if a.lower() == alias.strip().lower():
            return a
    raise ValueError(f"alias {alias!r} is not one of {', '.join(ALIASES)}")


def _from_row(row: dict[str, Any]) -> AgentVersion:
    return AgentVersion(
        version_id=row["version_id"],
        agent=row["agent"],
        manifest=row["manifest"],
        signed=bool(row.get("signature")),
    )


# -- signing -----------------------------------------------------------------------------------


def _statement(agent: str, version_id: str) -> bytes:
    return f"{_STATEMENT}\n{agent}\n{version_id}".encode()


def _sign(agent: str, version_id: str) -> tuple[str | None, str | None, str | None]:
    """``(signature, algo, key_id)``; all None when no signing key is configured."""
    from examlops import supplychain as sc

    try:
        private = sc._load_private_key()
    except Exception:  # noqa: BLE001 - a broken key is reported by signing_configured() callers
        private = None
    if private is not None:
        sig = base64.b64encode(private.sign(_statement(agent, version_id))).decode()
        return sig, sc.ED25519, sc.key_id(private.public_key())
    try:
        return sc._hmac_sign(version_id), sc.HMAC, None
    except sc.SigningKeyMissing:
        return None, None, None


def verify_signature(row: dict[str, Any]) -> bool:
    """Whether the stored signature over this row's ``version_id`` is valid."""
    from examlops import supplychain as sc

    sig, algo = row.get("signature"), row.get("sign_algo")
    if not sig:
        return False
    try:
        if algo == sc.HMAC:
            return hmac.compare_digest(sc._hmac_sign(row["version_id"]), sig)
        if algo == sc.ED25519:
            key = sc.trusted_public_keys().get(row.get("sign_key_id") or "")
            if key is None:
                return False
            key.verify(base64.b64decode(sig), _statement(row["agent"], row["version_id"]))  # type: ignore[attr-defined]
            return True
    except Exception:  # noqa: BLE001 - any failure to verify is "not verified"
        return False
    return False


# -- register / read ---------------------------------------------------------------------------


def register(doc: Any, *, actor: str | None = None) -> dict[str, Any]:
    """Validate and store a manifest; identical content returns the existing version.

    Raises :class:`AgentManifestError` when the manifest is invalid. A version is signed at
    registration when signing is configured; it is never re-written afterwards.
    """
    manifest = normalize(doc)
    vid = version_id_of(manifest)
    sig, algo, kid = _sign(manifest["agent"], vid)
    who = _actor(actor)
    created, row = store.insert_version(
        vid,
        manifest["agent"],
        canonical_json(manifest),
        actor=who,
        signature=sig,
        sign_algo=algo,
        sign_key_id=kid,
    )
    if created:
        audit_best_effort(
            _SOURCE,
            who,
            "agent_version_registered",
            f"{manifest['agent']}:{vid}",
            {"version_id": vid, "signed": bool(sig)},
        )
    return {
        "ok": True,
        "created": created,
        "version_id": vid,
        "agent": manifest["agent"],
        "signed": bool(row.get("signature")),
    }


def get(ref: str) -> dict[str, Any] | None:
    """A stored row by ``version_id`` or ``<agent>@<alias>``; None when unknown."""
    if "@" in ref and not ref.startswith("av-"):
        agent, alias = ref.rsplit("@", 1)
        try:
            a = store.get_alias(agent, canonical_alias(alias))
        except ValueError:
            return None
        return store.get_version(a["version_id"]) if a else None
    return store.get_version(ref)


def resolve(agent: str, alias: str = "Production") -> AgentVersion:
    """The version ``agent@alias`` points at. Raises ``LookupError`` when it points at nothing."""
    row = get(f"{agent}@{alias}")
    if row is None:
        raise LookupError(f"agent {agent!r} has no {alias} version")
    return _from_row(row)


def list_versions(agent: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    aliases: dict[str, list[str]] = {}
    for a in store.list_aliases(agent):
        aliases.setdefault(a["version_id"], []).append(a["alias"])
    out = []
    for r in store.list_versions(agent, limit=limit):
        out.append(
            {
                "version_id": r["version_id"],
                "agent": r["agent"],
                "aliases": sorted(aliases.get(r["version_id"], [])),
                "signed": bool(r.get("signature")),
                "created_at": r["created_at"],
                "actor": r["actor"],
            }
        )
    return out


def diff(ref_a: str, ref_b: str) -> dict[str, Any]:
    a, b = get(ref_a), get(ref_b)
    missing = [r for r, row in ((ref_a, a), (ref_b, b)) if row is None]
    if missing:
        raise LookupError(f"unknown agent version: {', '.join(missing)}")
    return diff_manifests(a["manifest"], b["manifest"])  # type: ignore[index]


# -- the promotion gate ------------------------------------------------------------------------


def model_key(agent: str) -> str:
    """The name the version's evaluation results are recorded under (ADR 0146 decision 1)."""
    return f"agent-{agent}"


def evidence_refusals(row: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """``(reasons, evidence)``; empty ``reasons`` means the evaluation evidence is sufficient.

    The check itself lives in :func:`examlops.evaluation.evidence.evaluation_evidence`, shared
    with the ADR 0159 GenAI-application registry so the platform has exactly one promotion gate
    rather than one per registry (ADR 0159 decision 3). This function is the agent-version
    *adapter* onto it: the ``agent-<name>`` key and the manifest's declared suites.
    """
    from examlops.evaluation.evidence import evaluation_evidence

    manifest = row["manifest"]
    declared = [s.split("@", 1)[0] for s in (manifest.get("eval") or {}).get("suites", [])]
    return evaluation_evidence(model_key(row["agent"]), row["version_id"], declared)


def _signature_refusals(row: dict[str, Any]) -> list[str]:
    from examlops import supplychain as sc

    if sc.signing_configured() is None and not row.get("signature"):
        return []  # a site that does not sign is not blocked
    if not row.get("signature"):
        return ["signing is configured but this version is unsigned; re-register it to sign"]
    if not verify_signature(row):
        return ["the manifest signature does not verify"]
    return []


def _slo_refusals(agent: str, evidence: dict[str, Any]) -> list[str]:
    """ADR 0148 d3: with the ``slo`` gate armed, a declared agentic SLOSpec must be ``met``.

    Off (the default): never consulted, nothing added to the evidence. An agent with no agentic
    spec is not refused here (this road only *includes* a spec's verdict when one exists).
    """
    from examlops.policy_engine.gates import consult
    from examlops.slo.specs import promotion_decision

    sink: dict[str, Any] = {}
    decision = consult(
        "slo",
        lambda _o: promotion_decision(agent, kinds=("agentic",), require_spec=False, sink=sink),
    )
    if decision is None:
        return []
    if sink.get("slo"):
        evidence["slo"] = sink["slo"]
    return [] if decision.allow else list(decision.reasons)


def _noninferiority_refusals(
    row: dict[str, Any], incumbent: dict[str, Any], evidence: dict[str, Any]
) -> list[str]:
    """ADR 0146 d3: the candidate must be non-inferior to the running Production version.

    Applied when the manifest declares ``eval.non_inferiority_margin``. Every gate metric that is
    a proportion over a known sample (it carries a Wilson interval) is tested with a one-sided
    Newcombe bound; a metric that is not a proportion has no per-sample data to test and is
    listed as ``untested`` - its regression is the gate's ``max_drop`` job, and saying so is
    better than inventing an interval. The measured difference is recorded either way.
    """
    from examlops.analysis.ab_stats import non_inferiority_proportions
    from examlops.data.evaluation import get_eval_gate, get_eval_results_for_agent_version

    margin = (row["manifest"].get("eval") or {}).get("non_inferiority_margin")
    if margin is None:
        evidence["non_inferiority"] = {"applied": False, "reason": "no margin declared"}
        return []
    key = model_key(row["agent"])
    gate = get_eval_gate(key)
    if gate is None:  # already refused by evaluation_evidence; nothing to test against
        return []

    def latest(vid: str) -> dict[str, dict[str, Any]]:
        # Filtered to this version in SQL and bounded, newest first with the row id breaking a
        # same-second tie - a re-run recorded in the same second as the run it replaces must
        # win, and the agent's whole evaluation history is never loaded to find two versions.
        out: dict[str, dict[str, Any]] = {}
        for r in get_eval_results_for_agent_version(
            row["agent"], vid, suite=gate["suite"], limit=1000
        ):
            out.setdefault(r["metric"], r)
        return out

    cand, base = latest(row["version_id"]), latest(incumbent["version_id"])
    default_up = gate.get("higher_is_better")
    results: list[dict[str, Any]] = []
    reasons: list[str] = []
    for m in gate["metrics"]:
        name = m["name"]
        up = bool(m.get("higher_is_better", True if default_up is None else default_up))
        c, b = cand.get(name), base.get(name)
        if c is None or b is None:
            who = "candidate" if c is None else "running Production version"
            reasons.append(f"non-inferiority on {name!r}: the {who} has no score")
            continue
        proportion = all(
            r.get("score_lo") is not None and int(r.get("sample_size") or 0) > 0 for r in (c, b)
        )
        if not proportion:
            results.append(
                {
                    "metric": name,
                    "tested": False,
                    "reason": "not a proportion over a known sample",
                    "difference": float(c["score"]) - float(b["score"]),
                }
            )
            continue
        nc, nb = int(c["sample_size"]), int(b["sample_size"])
        res = non_inferiority_proportions(
            round(float(c["score"]) * nc),
            nc,
            round(float(b["score"]) * nb),
            nb,
            margin=float(margin),
            higher_is_better=up,
        )
        results.append({"metric": name, "tested": True, **res})
        if not res["non_inferior"]:
            bound = res["lower"] if up else res["upper"]
            reasons.append(
                f"not non-inferior on {name!r}: difference {res['difference']:+.4f}, one-sided "
                f"bound {bound:+.4f} vs margin {margin} (running {incumbent['version_id']})"
            )
    evidence["non_inferiority"] = {
        "applied": True,
        "margin": float(margin),
        "incumbent": incumbent["version_id"],
        "metrics": results,
    }
    return reasons


def _state_refusals(
    row: dict[str, Any],
    incumbent: dict[str, Any],
    strategy: str | None,
    evidence: dict[str, Any],
) -> list[str]:
    """ADR 0146 d5: diff the checkpoint schemas; incompatible or inert needs pin/drain."""
    from examlops.agent_versions.compat import gate_state, state_compat

    result = state_compat(incumbent["manifest"].get("state"), row["manifest"].get("state"))
    evidence["state_compat"] = {
        **result,
        "from": incumbent["version_id"],
        "strategy": strategy,
    }
    return gate_state(result, strategy)


# -- alias moves -------------------------------------------------------------------------------


def set_alias(
    agent: str,
    alias: str,
    ref: str,
    *,
    actor: str | None = None,
    reason: str | None = None,
    state_strategy: str | None = None,
) -> dict[str, Any]:
    """Point ``agent@alias`` at a registered version. Production is gated on evidence.

    Replacing a running Production version additionally requires non-inferiority within the
    declared margin and a passing state-compatibility gate (``state_strategy`` = ``pin`` or
    ``drain`` for an incompatible or unknown checkpoint schema).

    Raises ``LookupError`` (unknown version / wrong agent), ``ValueError`` (bad alias) or
    :class:`GateRefusal`. A refusal is audited as ``agent_promotion_blocked``.
    """
    from examlops.agent_versions.compat import STATE_STRATEGIES

    alias = canonical_alias(alias)
    if state_strategy is not None and state_strategy not in STATE_STRATEGIES:
        raise ValueError(f"state strategy must be one of {', '.join(STATE_STRATEGIES)}")
    row = store.get_version(ref) if ref.startswith("av-") else get(ref)
    if row is None:
        raise LookupError(f"unknown agent version {ref!r}")
    if row["agent"] != agent:
        raise LookupError(f"version {row['version_id']} belongs to agent {row['agent']!r}")
    who = _actor(actor)
    evidence: dict[str, Any] = {}
    if alias == "Production":
        reasons, evidence = evidence_refusals(row)
        reasons += _signature_refusals(row)
        reasons += _slo_refusals(agent, evidence)
        current = store.get_alias(agent, "Production")
        incumbent = store.get_version(current["version_id"]) if current else None
        if incumbent is not None and incumbent["version_id"] != row["version_id"]:
            reasons += _noninferiority_refusals(row, incumbent, evidence)
            reasons += _state_refusals(row, incumbent, state_strategy, evidence)
        if reasons:
            audit_best_effort(
                _SOURCE,
                who,
                "agent_promotion_blocked",
                f"{agent}@{alias}",
                {"version_id": row["version_id"], "reasons": reasons},
            )
            raise GateRefusal(reasons)
    prev = store.move_alias(
        agent,
        alias,
        row["version_id"],
        action="set",
        actor=who,
        reason=reason,
        evidence=evidence or None,
    )
    audit_best_effort(
        _SOURCE,
        who,
        "agent_alias_moved",
        f"{agent}@{alias}",
        {"version_id": row["version_id"], "previous": prev, "reason": reason, "evidence": evidence},
    )
    return {
        "ok": True,
        "agent": agent,
        "alias": alias,
        "version_id": row["version_id"],
        "previous": prev,
        "evidence": evidence,
    }


#: What an agent runtime does with in-flight sessions on a rolled-back version (ADR 0146 d4).
IN_FLIGHT_POLICIES = ("continue", "interrupt", "quarantine")


def rollback(
    agent: str,
    alias: str,
    *,
    actor: str | None = None,
    reason: str | None = None,
    in_flight: str = "continue",
) -> dict[str, Any]:
    """Move ``agent@alias`` back to where the latest move found it (ADR 0146 decision 4).

    Not re-gated: the target already passed this gate when it was promoted. ``in_flight`` is
    what the agent runtime does with sessions already running on the version being rolled back
    (``continue`` | ``interrupt`` | ``quarantine``); it is recorded on the move and reaches the
    runtime through the agent snapshot. Raises ``LookupError`` when there is nothing to roll
    back to and ``ValueError`` for an unknown policy.
    """
    if in_flight not in IN_FLIGHT_POLICIES:
        raise ValueError(f"in-flight policy must be one of {', '.join(IN_FLIGHT_POLICIES)}")
    alias = canonical_alias(alias)
    hist = store.alias_history(agent, alias, limit=1)
    if not hist or not hist[0]["prev_version"]:
        raise LookupError(f"{agent}@{alias} has no earlier version to roll back to")
    target = hist[0]["prev_version"]
    if store.get_version(target) is None:
        raise LookupError(f"rollback target {target} is no longer registered")
    who = _actor(actor)
    prev = store.move_alias(
        agent,
        alias,
        target,
        action="rollback",
        actor=who,
        reason=reason,
        evidence={"in_flight": in_flight},
    )
    audit_best_effort(
        _SOURCE,
        who,
        "agent_alias_rolled_back",
        f"{agent}@{alias}",
        {"version_id": target, "from": prev, "reason": reason, "in_flight": in_flight},
    )
    return {
        "ok": True,
        "agent": agent,
        "alias": alias,
        "version_id": target,
        "previous": prev,
        "in_flight": in_flight,
    }


def set_canary(
    agent: str, percent: float, *, actor: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Start ``percent`` % of the agent's NEW sessions on its Canary version (ADR 0146 d4).

    Existing sessions never change version - the runtime pins each session to the version it
    started on. A share above 0 requires a Canary alias to exist. Raises ``ValueError`` for a
    share outside ``[0, 100]`` and ``LookupError`` when there is no Canary version to send to.
    """
    import math

    if not isinstance(percent, (int, float)) or not math.isfinite(percent):
        raise ValueError("canary percent must be a finite number")
    if not 0 <= float(percent) <= 100:
        raise ValueError("canary percent must lie in [0, 100]")
    if percent > 0 and store.get_alias(agent, "Canary") is None:
        raise LookupError(f"{agent} has no Canary version (exa agent alias set {agent} Canary ...)")
    who = _actor(actor)
    prev = store.set_rollout(agent, float(percent), actor=who)
    audit_best_effort(
        _SOURCE,
        who,
        "agent_canary_set",
        f"{agent}@Canary",
        {"canary_percent": float(percent), "previous": prev, "reason": reason},
    )
    return {"ok": True, "agent": agent, "canary_percent": float(percent), "previous": prev}


__all__ += ["IN_FLIGHT_POLICIES", "AgentManifestError", "model_key", "set_canary"]
