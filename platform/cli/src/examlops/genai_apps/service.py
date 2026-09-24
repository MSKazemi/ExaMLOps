"""Register, alias, promote and resolve GenAI applications (ADR 0159).

The promotion gate is the platform's one gate, not a second one: ``Production`` runs the same
:func:`examlops.evaluation.evidence.evaluation_evidence` an agent version runs (and a predictive
model's ``exa eval gate`` before it), keyed ``genai-app-<name>`` the way an agent version is keyed
``agent-<name>``. The judge-calibration rule of ADR 0111 therefore applies here because it is the
same code, not because it was re-typed here.

Two refusals are specific to this registry, because a published application surface fails
differently from a model (ADR 0159 decision 3):

* a ``Production`` application may not carry ``guardrail.mode: off`` - a route or an agent may
  reasonably run unguarded in a controlled context; an application answering end-user traffic
  may not;
* every *declared* component must currently resolve (:mod:`examlops.genai_apps.components`), so a
  version naming a knowledge base that has since been deleted is refused with a named reason
  instead of promoted to fail on its first real call.

Rolling back is different: it restores a version that already passed this gate, so it is not
re-gated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from examlops.data import genai_apps as store
from examlops.data.audit import audit_best_effort
from examlops.genai_apps.components import component_refusals
from examlops.genai_apps.manifest import (
    ALIASES,
    GenAIAppManifestError,
    canonical_json,
    diff_manifests,
    normalize,
    version_id_of,
)

__all__ = [
    "ALIASES",
    "GateRefusal",
    "GenAIAppManifestError",
    "GenAIApplication",
    "canonical_alias",
    "diff",
    "evidence_refusals",
    "get",
    "list_apps",
    "model_key",
    "register",
    "resolve",
    "rollback",
    "set_alias",
]

_SOURCE = "genai-apps"
_ID_PREFIX = "gaa-"


class GateRefusal(RuntimeError):
    """A promotion was refused; ``reasons`` names each unmet condition."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class GenAIApplication:
    version_id: str
    name: str
    manifest: dict[str, Any]

    @property
    def route_model(self) -> str:
        return str(self.manifest["route"]["model"])

    @property
    def guardrail_mode(self) -> str:
        return str(self.manifest["guardrail"]["mode"])


def _actor(actor: str | None) -> str:
    return actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"


def canonical_alias(alias: str) -> str:
    for a in ALIASES:
        if a.lower() == alias.strip().lower():
            return a
    raise ValueError(f"alias {alias!r} is not one of {', '.join(ALIASES)}")


def _from_row(row: dict[str, Any]) -> GenAIApplication:
    return GenAIApplication(
        version_id=row["version_id"], name=row["name"], manifest=row["manifest"]
    )


# -- register / read ---------------------------------------------------------------------------


def register(doc: Any, *, actor: str | None = None) -> dict[str, Any]:
    """Validate and store a manifest; identical content returns the existing version.

    Raises :class:`GenAIAppManifestError` when the manifest is invalid.
    """
    manifest = normalize(doc)
    vid = version_id_of(manifest)
    who = _actor(actor)
    created, _row = store.insert_version(vid, manifest["name"], canonical_json(manifest), actor=who)
    if created:
        audit_best_effort(
            _SOURCE,
            who,
            "genai_app_registered",
            f"{manifest['name']}:{vid}",
            {"version_id": vid, "route": manifest["route"]["model"]},
        )
    return {"ok": True, "created": created, "version_id": vid, "name": manifest["name"]}


def get(ref: str) -> dict[str, Any] | None:
    """A stored row by ``version_id`` or ``<name>@<alias>``; None when unknown."""
    if "@" in ref and not ref.startswith(_ID_PREFIX):
        name, alias = ref.rsplit("@", 1)
        try:
            a = store.get_alias(name, canonical_alias(alias))
        except ValueError:
            return None
        return store.get_version(a["version_id"]) if a else None
    return store.get_version(ref)


def resolve(name: str, alias: str = "Production") -> GenAIApplication:
    """The version ``name@alias`` points at. Raises ``LookupError`` when it points at nothing."""
    row = get(f"{name}@{alias}")
    if row is None:
        raise LookupError(f"genai application {name!r} has no {alias} version")
    return _from_row(row)


def list_apps(name: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    aliases: dict[str, list[str]] = {}
    for a in store.list_aliases(name):
        aliases.setdefault(a["version_id"], []).append(a["alias"])
    out = []
    for r in store.list_versions(name, limit=limit):
        m = r["manifest"]
        out.append(
            {
                "version_id": r["version_id"],
                "name": r["name"],
                "aliases": sorted(aliases.get(r["version_id"], [])),
                "route": m["route"]["model"],
                "rag": (m.get("rag") or {}).get("kb"),
                "guardrail": m["guardrail"]["mode"],
                "created_at": r["created_at"],
                "actor": r["actor"],
            }
        )
    return out


def diff(ref_a: str, ref_b: str) -> dict[str, Any]:
    a, b = get(ref_a), get(ref_b)
    missing = [r for r, row in ((ref_a, a), (ref_b, b)) if row is None]
    if missing:
        raise LookupError(f"unknown genai application version: {', '.join(missing)}")
    return diff_manifests(a["manifest"], b["manifest"])  # type: ignore[index]


# -- the promotion gate ------------------------------------------------------------------------


def model_key(name: str) -> str:
    """The name this application's evaluation results are recorded under (ADR 0159 decision 3)."""
    return f"genai-app-{name}"


def evidence_refusals(row: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """``(reasons, evidence)``; empty ``reasons`` means the evaluation evidence is sufficient.

    The adapter onto the platform's shared gate - the ``genai-app-<name>`` key and this manifest's
    declared suites. The check itself is
    :func:`examlops.evaluation.evidence.evaluation_evidence`, the same function the agent-version
    registry calls.
    """
    from examlops.evaluation.evidence import evaluation_evidence

    manifest = row["manifest"]
    declared = [s.split("@", 1)[0] for s in (manifest.get("eval") or {}).get("suites", [])]
    return evaluation_evidence(model_key(row["name"]), row["version_id"], declared)


def _guardrail_refusals(manifest: dict[str, Any]) -> list[str]:
    """ADR 0159 decision 3: a published application surface may not run with guardrails off."""
    if manifest["guardrail"]["mode"] == "off":
        return [
            "guardrail.mode is 'off': a Production GenAI application may not run unguarded "
            "(ADR 0159 decision 3) - register a version with mode 'monitor' or 'enforce'"
        ]
    return []


def _component_refusals(manifest: dict[str, Any]) -> list[str]:
    return [str(r) for r in component_refusals(manifest)]


# -- alias moves -------------------------------------------------------------------------------


def set_alias(
    name: str,
    alias: str,
    ref: str,
    *,
    actor: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Point ``name@alias`` at a registered version. Production is gated.

    Raises ``LookupError`` (unknown version / wrong application), ``ValueError`` (bad alias) or
    :class:`GateRefusal`. A refusal is audited as ``genai_app_promotion_blocked``.
    """
    alias = canonical_alias(alias)
    row = store.get_version(ref) if ref.startswith(_ID_PREFIX) else get(ref)
    if row is None:
        raise LookupError(f"unknown genai application version {ref!r}")
    if row["name"] != name:
        raise LookupError(f"version {row['version_id']} belongs to application {row['name']!r}")
    who = _actor(actor)
    evidence: dict[str, Any] = {}
    if alias == "Production":
        reasons, evidence = evidence_refusals(row)
        reasons += _guardrail_refusals(row["manifest"])
        reasons += _component_refusals(row["manifest"])
        if reasons:
            audit_best_effort(
                _SOURCE,
                who,
                "genai_app_promotion_blocked",
                f"{name}@{alias}",
                {"version_id": row["version_id"], "reasons": reasons},
            )
            raise GateRefusal(reasons)
    prev = store.move_alias(
        name,
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
        "genai_app_alias_moved",
        f"{name}@{alias}",
        {"version_id": row["version_id"], "previous": prev, "reason": reason, "evidence": evidence},
    )
    return {
        "ok": True,
        "name": name,
        "alias": alias,
        "version_id": row["version_id"],
        "previous": prev,
        "evidence": evidence,
    }


def rollback(
    name: str, alias: str, *, actor: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Move ``name@alias`` back to where the latest move found it.

    Not re-gated: the target already passed this gate when it was promoted. Raises ``LookupError``
    when there is nothing to roll back to.
    """
    alias = canonical_alias(alias)
    hist = store.alias_history(name, alias, limit=1)
    if not hist or not hist[0]["prev_version"]:
        raise LookupError(f"{name}@{alias} has no earlier version to roll back to")
    target = hist[0]["prev_version"]
    if store.get_version(target) is None:
        raise LookupError(f"rollback target {target} is no longer registered")
    who = _actor(actor)
    prev = store.move_alias(name, alias, target, action="rollback", actor=who, reason=reason)
    audit_best_effort(
        _SOURCE,
        who,
        "genai_app_alias_rolled_back",
        f"{name}@{alias}",
        {"version_id": target, "from": prev, "reason": reason},
    )
    return {"ok": True, "name": name, "alias": alias, "version_id": target, "previous": prev}
