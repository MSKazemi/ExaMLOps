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


# -- alias moves -------------------------------------------------------------------------------


def set_alias(
    agent: str,
    alias: str,
    ref: str,
    *,
    actor: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Point ``agent@alias`` at a registered version. Production is gated on evidence.

    Raises ``LookupError`` (unknown version / wrong agent), ``ValueError`` (bad alias) or
    :class:`GateRefusal`. A refusal is audited as ``agent_promotion_blocked``.
    """
    alias = canonical_alias(alias)
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


def rollback(
    agent: str, alias: str, *, actor: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Move ``agent@alias`` back to where the latest move found it (ADR 0146 decision 4).

    Not re-gated: the target already passed this gate when it was promoted. Raises ``LookupError``
    when there is nothing to roll back to.
    """
    alias = canonical_alias(alias)
    hist = store.alias_history(agent, alias, limit=1)
    if not hist or not hist[0]["prev_version"]:
        raise LookupError(f"{agent}@{alias} has no earlier version to roll back to")
    target = hist[0]["prev_version"]
    if store.get_version(target) is None:
        raise LookupError(f"rollback target {target} is no longer registered")
    who = _actor(actor)
    prev = store.move_alias(agent, alias, target, action="rollback", actor=who, reason=reason)
    audit_best_effort(
        _SOURCE,
        who,
        "agent_alias_rolled_back",
        f"{agent}@{alias}",
        {"version_id": target, "from": prev, "reason": reason},
    )
    return {"ok": True, "agent": agent, "alias": alias, "version_id": target, "previous": prev}


__all__ += ["AgentManifestError", "model_key"]
