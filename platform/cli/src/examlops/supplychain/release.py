"""The supply-chain release gate and attestation (ADR 0013 clauses 2–5).

``check_release`` answers one question for CI and for promotion: *does this model version carry
the supply-chain evidence a release needs?* Three independent requirements, each fail-closed:

``signature``
    A ``model_signatures`` row exists **and** is a valid Ed25519 / Sigstore signature, by a trusted
    signer, over its recorded manifest digest. (Whether the bytes still match that digest is
    verify-before-load's job at serving time.) A legacy HMAC row does not qualify.
``bom``
    A CycloneDX AI-BOM is recorded, carries ``examlops:artifact_digest``, and that digest equals
    the verified signature's digest (or, unsigned, the verified provenance subject) — a BOM that
    names no bytes, or describes other bytes, is not this version's BOM (BOM integrity).
``provenance``
    SLSA v1 provenance is recorded, its signature verifies, and its subject digest equals the
    signature's digest.

``attest_version`` records the evidence — optional signature, AI-BOM bound to the artifact
digest, signed provenance — for one version; the training pipeline calls it at registration and
``exa models attest`` calls it for versions registered before this existed.

``release_gate_decision`` adapts the check to the policy engine's ``supply_chain`` gate: arming
it with ``require: [signature, bom, provenance]`` makes ``exa pipeline promote`` refuse a version
that lacks any of them. The default requirement (``signature``) keeps the gate's historical
meaning.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REQUIREMENTS = ("signature", "bom", "provenance")
_MANIFEST_SCHEMES = ("ed25519-v2", "sigstore-v1")


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    reason: str


@dataclass(frozen=True)
class ReleaseCheck:
    model: str
    version: str
    ok: bool
    findings: list[Finding] = field(default_factory=list)

    def failures(self) -> list[str]:
        return [f"{f.check}: {f.reason}" for f in self.findings if not f.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "version": self.version,
            "ok": self.ok,
            "findings": [asdict(f) for f in self.findings],
        }


@dataclass(frozen=True)
class AttestResult:
    model: str
    version: str
    subject_digest: str
    signature_algo: str | None
    provenance_algo: str
    bom_recorded: bool


def normalize_requirements(require: Iterable[str] | str | None) -> tuple[str, ...]:
    """Validate a requirement list. Unknown names raise: a typo must not drop a check."""
    if require is None:
        return REQUIREMENTS
    items = require.split(",") if isinstance(require, str) else list(require)
    out: list[str] = []
    for item in items:
        name = str(item).strip().lower()
        if not name:
            continue
        if name not in REQUIREMENTS:
            raise ValueError(f"unknown release requirement {name!r} (expected {REQUIREMENTS})")
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError("at least one release requirement is needed")
    return tuple(out)


def _boms(model: str, version: str) -> list[dict[str, Any]]:
    """Every AI-BOM recorded for ``model@version``, whatever casing it was written under.

    ``model_boms`` is keyed by the model name as typed, so ``JPCP`` (``exa models attest``) and
    ``jpcp`` (the registered name) can each hold one. Reading only the newest would let a stale or
    conflicting BOM hide behind a timestamp tie, so the gate looks at all of them.
    """
    from examlops.data import get_db, init_db  # noqa: PLC0415

    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT bom_json FROM model_boms WHERE lower(model)=lower(?) AND version=? "
            "ORDER BY created_at DESC, rowid DESC",
            (model, str(version)),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            doc = json.loads(row["bom_json"])
        except (TypeError, ValueError):
            doc = {}
        out.append(doc if isinstance(doc, dict) else {})
    return out


def _bom_digest(bom: dict[str, Any]) -> str | None:
    for prop in bom.get("properties") or []:
        if isinstance(prop, dict) and prop.get("name") == "examlops:artifact_digest":
            value = prop.get("value")
            return str(value) if value else None
    return None


def _bom_finding(boms: list[dict[str, Any]], reference: str | None) -> Finding:
    """BOM integrity: recorded, bound to an artifact digest, and consistent with the reference.

    ``reference`` is the digest the evidence must describe — the verified signature's digest,
    else the verified provenance subject. Fail-closed on every ambiguity: an unbound BOM, two
    BOMs binding different digests, or a BOM describing bytes other than the reference.
    """
    if not boms:
        return Finding("bom", False, "missing: no AI-BOM recorded")
    digests = {d for d in (_bom_digest(b) for b in boms) if d}
    if not digests:
        return Finding(
            "bom", False, "unbound: the AI-BOM names no artifact digest (re-run exa models attest)"
        )
    if len(digests) > 1:
        return Finding("bom", False, "bom-mismatch: recorded AI-BOMs bind different digests")
    (bound,) = digests
    if reference is not None and bound != reference:
        return Finding("bom", False, "bom-mismatch: BOM describes different artifact bytes")
    if reference is None:
        return Finding(
            "bom", True, "recorded and bound (no verified signature or provenance to compare)"
        )
    return Finding("bom", True, "recorded and bound to the verified artifact digest")


def check_release(
    model: str, version: str, *, require: Iterable[str] | str | None = None
) -> ReleaseCheck:
    """Evaluate the release requirements for ``model@version`` (read-only).

    The signature is checked cryptographically over its recorded digest (a row that is merely
    present is not a signature), and only a *verified* digest is used to bind the BOM and the
    provenance to the same bytes.
    """
    from examlops import supplychain  # noqa: PLC0415
    from examlops.data.registry import get_model_signature  # noqa: PLC0415
    from examlops.supplychain.provenance import verify_provenance  # noqa: PLC0415

    needed = normalize_requirements(require)
    version = str(version)
    sig = get_model_signature(model, version)
    sig_verdict = supplychain.verify_record_signature(sig, model, version) if sig else None
    signed_digest = sig_verdict.digest if sig_verdict is not None and sig_verdict.ok else None
    prov = None
    if "provenance" in needed or "bom" in needed:
        prov = verify_provenance(model, version, expected_digest=signed_digest)
    findings: list[Finding] = []
    if "signature" in needed:
        if sig is None or sig_verdict is None:
            findings.append(Finding("signature", False, "unsigned: no signature on record"))
        elif not sig_verdict.ok:
            findings.append(Finding("signature", False, sig_verdict.reason))
        else:
            findings.append(Finding("signature", True, f"signed and verified ({sig.get('algo')})"))
    if "bom" in needed:
        reference = signed_digest
        if reference is None and prov is not None and prov.ok and prov.statement:
            reference = "sha256:" + str(prov.statement["subject"][0]["digest"]["sha256"])
        findings.append(_bom_finding(_boms(model, version), reference))
    if "provenance" in needed and prov is not None:
        findings.append(Finding("provenance", prov.ok, prov.reason))
    return ReleaseCheck(model, version, all(f.ok for f in findings), findings)


def release_gate_decision(model: str, version: str, require: Sequence[str] | str | None) -> Any:
    """An ``EngineDecision`` for the ``supply_chain`` policy gate.

    With no ``require`` option the gate keeps its historical meaning (``signature`` only, then
    the policy bundle). A malformed ``require`` denies — fail-closed.
    """
    from examlops.policy_engine import EngineDecision, supply_chain_gate  # noqa: PLC0415

    try:
        needed = normalize_requirements(require) if require else ("signature",)
    except ValueError as exc:
        return EngineDecision(False, [f"supply_chain gate misconfigured: {exc}"], "deny", "builtin")
    result = check_release(model, version, require=needed)
    if not result.ok:
        return EngineDecision(
            False,
            [f"{model}/{version} release evidence incomplete — {r}" for r in result.failures()],
            "deny",
            "builtin",
        )
    return supply_chain_gate(model, version, signed=True)


def promotion_decision(model: str, version: str | None, opts: dict[str, Any]) -> Any:
    """The armed ``supply_chain`` gate's decision for promoting ``model@version``.

    One function for every promotion road (``exa pipeline promote`` and the autopilot), so an
    armed gate cannot be met on one road and skipped on the other. ``require`` in the gate's
    options selects the evidence (ADR 0013); without it the gate keeps its historical meaning
    (a signature row must exist). An unresolvable version denies — fail-closed.
    """
    from examlops.data.registry import get_model_signature  # noqa: PLC0415
    from examlops.policy_engine import EngineDecision, supply_chain_gate  # noqa: PLC0415

    if not version:
        return EngineDecision(
            False,
            [f"{model}: the candidate version could not be resolved for the supply_chain gate"],
            "deny",
            "builtin",
        )
    if opts.get("require"):
        return release_gate_decision(model, str(version), opts["require"])
    signed = get_model_signature(model, str(version)) is not None
    return supply_chain_gate(model, str(version), signed=signed)


def attest_version(
    model: str,
    version: str,
    artifact_paths: Iterable[Path],
    *,
    root: Path | None = None,
    ctx: Any = None,
    sign: bool = False,
    actor: str | None = None,
    replace: bool = False,
) -> AttestResult:
    """Record the release evidence for one version from its artifact bytes.

    ``sign`` also (re-)signs the version; otherwise an existing manifest-digest signature must
    match the artifacts, so provenance can never be recorded for bytes other than the signed ones.
    """
    from examlops import supplychain  # noqa: PLC0415
    from examlops.data.registry import get_model_signature  # noqa: PLC0415
    from examlops.supplychain.provenance import BuildContext, record_provenance  # noqa: PLC0415

    paths = [Path(p) for p in artifact_paths]
    if not any(p.is_file() for p in paths):
        raise ValueError(f"{model}@{version}: no artifact files to attest")
    digest = supplychain.manifest_digest(paths, root=root)
    sig_algo: str | None = None
    if sign:
        sig_algo = supplychain.sign_model(model, version, paths, actor=actor, root=root).algo
    else:
        existing = get_model_signature(model, version)
        if existing is not None:
            sig_algo = existing.get("algo")
            if sig_algo in _MANIFEST_SCHEMES and existing["digest"] != digest:
                raise ValueError(
                    f"{model}@{version}: artifacts do not match the signed digest — refusing to "
                    "attest bytes other than the signed ones"
                )
    context = ctx if ctx is not None else BuildContext.from_env()
    supplychain.generate_ai_bom(
        model,
        version,
        dataset=context.dataset,
        dataset_revision=context.dataset_revision,
        framework=context.framework,
        artifact_digest=digest,
    )
    record = record_provenance(model, version, digest, context, actor=actor, replace=replace)
    return AttestResult(model, version, digest, sig_algo, record.algo, True)


def attest_registered_version(
    model: str,
    version: str,
    *,
    ctx: Any = None,
    sign: bool = False,
    actor: str | None = None,
    replace: bool = False,
) -> AttestResult:
    """``attest_version`` over the registered artifacts, downloaded as serving downloads them."""
    from examlops import supplychain  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="examlops-attest-") as tmp:
        local = supplychain.registered_artifacts(model, version, Path(tmp))
        paths = [p for p in local.rglob("*") if p.is_file()]
        return attest_version(
            model, version, paths, root=local, ctx=ctx, sign=sign, actor=actor, replace=replace
        )


def record_evidence_for_digest(
    model: str, version: str, digest: str, *, ctx: Any = None, actor: str | None = None
) -> AttestResult:
    """Record BOM + provenance for a digest already computed by signing (no second download)."""
    from examlops import supplychain  # noqa: PLC0415
    from examlops.supplychain.provenance import BuildContext, record_provenance  # noqa: PLC0415

    context = ctx if ctx is not None else BuildContext.from_env()
    supplychain.generate_ai_bom(
        model,
        version,
        dataset=context.dataset,
        dataset_revision=context.dataset_revision,
        framework=context.framework,
        artifact_digest=digest,
    )
    record = record_provenance(model, version, digest, context, actor=actor)
    return AttestResult(model, version, digest, None, record.algo, True)
