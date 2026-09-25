"""SLSA v1 build provenance for model versions (ADR 0013 clause 3).

For each registered model version the training pipeline (or ``exa models attest``) records *how*
it was built: an in-toto **Statement v1** whose subject is the version's artifact manifest digest
(the same digest the model signature covers) and whose predicate is **SLSA Provenance v1** — the
build's external parameters (model, dataset, pinned dataset revision, backend), its resolved
dependencies (the dataset revision, the source commit) and run details (builder id, MLflow run id,
HPC job id and scheduler).

The statement is wrapped in a **DSSE** envelope and signed:

``ed25519-dsse``
    With the platform's Ed25519 signing key (the same key pair and trust bundle as model
    signatures; PAE-encoded per the DSSE v1 spec).
``sigstore-v1``
    Keyless, when ``EXAMLOPS_SIGNING_SCHEME=sigstore``: a Sigstore bundle holding the DSSE
    envelope, a Fulcio certificate and the **Rekor** inclusion proof — the transparency-log anchor.
``none``
    No key configured. The statement is still recorded (it documents the build) but it is not
    evidence: :func:`verify_provenance` answers ``unsigned`` and the release gate refuses it.

Every record is also anchored in the hash-chained audit trail (D4): the ``model_provenance_recorded``
event carries the SHA-256 of the exact envelope stored, so a later rewrite of the row is detectable
from the chain (and from the external WORM anchor when one is configured).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
PAYLOAD_TYPE = "application/vnd.in-toto+json"
BUILD_TYPE = "https://github.com/MSKazemi/ExaMLOps/blob/main/docs/guides/supply-chain-security.md#slsa-build-type-training-v1"
DEFAULT_BUILDER = "urn:examlops:builder:training-pipeline"

ED25519_DSSE = "ed25519-dsse"
SIGSTORE = "sigstore-v1"
UNSIGNED = "none"

# Bound what a caller can push into a provenance row: parameters are operator/pipeline input.
_MAX_PARAMS = 64
_MAX_VALUE_LEN = 1024


class ProvenanceExists(RuntimeError):
    """A different provenance statement is already recorded for this version."""


@dataclass(frozen=True)
class BuildContext:
    """What the training run knew about itself. Every field is optional; unknown stays unknown."""

    run_id: str | None = None
    dataset: str | None = None
    dataset_revision: str | None = None
    backend: str | None = None
    framework: str | None = None
    job_id: str | None = None
    scheduler: str | None = None
    source_repository: str | None = None
    source_commit: str | None = None
    builder_id: str | None = None
    started_on: str | None = None
    finished_on: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides: Any) -> BuildContext:
        """Fill source/builder fields from the CI environment when the caller did not."""
        env_commit = (
            os.getenv("EXAMLOPS_SOURCE_COMMIT")
            or os.getenv("GITHUB_SHA")
            or os.getenv("CI_COMMIT_SHA")
            or None
        )
        env_repo = os.getenv("EXAMLOPS_SOURCE_REPOSITORY") or None
        base: dict[str, Any] = {
            "source_commit": env_commit,
            "source_repository": env_repo,
            "builder_id": os.getenv("EXAMLOPS_BUILDER_ID") or None,
            "dataset_revision": os.getenv("EXAMLOPS_DATASET_REVISION") or None,
        }
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**base)


@dataclass(frozen=True)
class ProvenanceRecord:
    model: str
    version: str
    subject_digest: str
    algo: str
    key_id: str | None
    statement_sha256: str
    envelope: dict[str, Any]


@dataclass(frozen=True)
class ProvenanceVerdict:
    ok: bool
    reason: str
    statement: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _norm_model(model: str) -> str:
    return model.strip().lower()


def subject_name(model: str, version: str) -> str:
    return f"models:/{_norm_model(model)}/{version}"


def _hex_of(digest: str) -> str:
    algo, sep, value = digest.partition(":")
    if not sep or algo != "sha256" or len(value) != 64:
        raise ValueError(f"subject digest must be 'sha256:<64 hex>', got {digest!r}")
    int(value, 16)  # raises on non-hex
    return value


def _clean_params(params: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in sorted(params)[:_MAX_PARAMS]:
        out[str(key)[:128]] = str(params[key])[:_MAX_VALUE_LEN]
    return out


def build_statement(
    model: str, version: str, subject_digest: str, ctx: BuildContext
) -> dict[str, Any]:
    """The in-toto Statement v1 with an SLSA Provenance v1 predicate for one model version."""
    external: dict[str, Any] = {
        "model": _norm_model(model),
        "version": str(version),
        "dataset": ctx.dataset,
        "datasetRevision": ctx.dataset_revision,
        "backend": ctx.backend,
        "framework": ctx.framework,
    }
    if ctx.parameters:
        external["parameters"] = _clean_params(ctx.parameters)
    deps: list[dict[str, Any]] = []
    if ctx.dataset:
        dep: dict[str, Any] = {"uri": f"dataset:{ctx.dataset}", "name": ctx.dataset}
        if ctx.dataset_revision:
            dep["annotations"] = {"revision": ctx.dataset_revision}
        deps.append(dep)
    if ctx.source_commit:
        uri = ctx.source_repository or "git"
        deps.append({"uri": f"git+{uri}", "digest": {"gitCommit": ctx.source_commit}})
    byproducts: list[dict[str, Any]] = []
    if ctx.job_id:
        byproducts.append(
            {"name": "hpc-job", "annotations": {"jobId": ctx.job_id, "scheduler": ctx.scheduler}}
        )
    metadata: dict[str, Any] = {"invocationId": ctx.run_id or "unknown"}
    if ctx.started_on:
        metadata["startedOn"] = ctx.started_on
    metadata["finishedOn"] = ctx.finished_on or _now()
    return {
        "_type": STATEMENT_TYPE,
        "subject": [
            {"name": subject_name(model, version), "digest": {"sha256": _hex_of(subject_digest)}}
        ],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {k: v for k, v in external.items() if v is not None},
                "internalParameters": {"examlops": _examlops_version()},
                "resolvedDependencies": deps,
            },
            "runDetails": {
                "builder": {"id": ctx.builder_id or DEFAULT_BUILDER},
                "metadata": metadata,
                "byproducts": byproducts,
            },
        },
    }


def _examlops_version() -> str:
    try:
        import importlib.metadata as md  # noqa: PLC0415

        return md.version("examlops")
    except Exception:  # noqa: BLE001
        return "unknown"


def canonical(statement: dict[str, Any]) -> bytes:
    return json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()


def pae(payload_type: str, body: bytes) -> bytes:
    """DSSE v1 Pre-Authentication Encoding — what the signature actually covers."""
    t = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(body), body)


def _envelope(
    statement: dict[str, Any], *, actor: str | None = None
) -> tuple[dict[str, Any], str, str | None]:
    """Sign ``statement``: ``(envelope, algo, key id / signer identity)``."""
    from examlops import supplychain  # noqa: PLC0415

    body = canonical(statement)
    if supplychain.signing_scheme() == "sigstore":
        from examlops.supplychain import keyless  # noqa: PLC0415

        try:
            bundle, identity = keyless.sign_statement(body)
            return {"sigstoreBundle": json.loads(bundle)}, SIGSTORE, identity
        except Exception as exc:  # noqa: BLE001 - fall back to the Ed25519 key, visibly
            logger.warning("keyless provenance signing failed (%s); trying Ed25519", exc)
            # Audited like the model-signature fallback: a site that asked for keyless and got a
            # key-signed (or unsigned) statement must be able to see that it happened.
            from examlops.data.audit import audit_best_effort  # noqa: PLC0415

            audit_best_effort(
                "exa-supplychain",
                actor,
                "model_provenance_keyless_fallback",
                str(statement.get("subject", [{}])[0].get("name", "")),
                {"reason": f"{type(exc).__name__}: {exc}"[:300]},
            )
    envelope: dict[str, Any] = {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(body).decode(),
        "signatures": [],
    }
    signed = supplychain.ed25519_sign_bytes(pae(PAYLOAD_TYPE, body))
    if signed is None:
        return envelope, UNSIGNED, None
    sig, kid = signed
    envelope["signatures"].append({"keyid": kid, "sig": sig})
    return envelope, ED25519_DSSE, kid


def record_provenance(
    model: str,
    version: str,
    subject_digest: str,
    ctx: BuildContext,
    *,
    actor: str | None = None,
    replace: bool = False,
) -> ProvenanceRecord:
    """Build, sign, store and audit-anchor the provenance of ``model@version``.

    Idempotent for the same build: re-recording an identical statement is a no-op. A *different*
    statement for a version that already has one raises :class:`ProvenanceExists` unless
    ``replace`` — provenance is evidence, and overwriting evidence is a deliberate act.
    """
    statement = build_statement(model, version, subject_digest, ctx)
    # The finishedOn timestamp differs between two calls for the same build, so idempotency is
    # judged on the statement minus run metadata timestamps.
    fingerprint = _fingerprint(statement)
    existing = _get_row(model, version)
    upgrade = False
    if existing is not None and not replace:
        _same_or_raise(model, version, existing, fingerprint)
        if existing["algo"] != UNSIGNED:
            return _record_from_row(existing)
        # The same build was recorded unsigned (no key at the time). Signing it now is not an
        # overwrite of evidence — it adds the signature the record lacked — so it needs no replace.
        upgrade = True
    envelope, algo, kid = _envelope(statement, actor=actor)
    if upgrade and algo == UNSIGNED:
        assert existing is not None
        return _record_from_row(existing)
    statement_sha = hashlib.sha256(canonical(statement)).hexdigest()
    envelope_json = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    builder = statement["predicate"]["runDetails"]["builder"]["id"]
    written = _store_row(
        model,
        version,
        subject_digest,
        statement_sha,
        algo,
        kid,
        envelope_json,
        builder,
        actor,
        mode="replace" if replace else ("upgrade" if upgrade else "insert"),
    )
    if not written:
        # A concurrent recorder won the row between our read and our write. Never overwrite it
        # silently: the same build is a no-op, a different one is the ProvenanceExists it would
        # have been had we read a moment later.
        current = _get_row(model, version)
        if current is None:  # pragma: no cover - a row that vanished mid-flight
            raise ProvenanceExists(f"{model}@{version}: provenance row changed concurrently")
        _same_or_raise(model, version, current, fingerprint)
        return _record_from_row(current)
    from examlops.data.audit import audit_best_effort  # noqa: PLC0415

    audit_best_effort(
        "exa-supplychain",
        actor,
        "model_provenance_recorded",
        f"{model}@{version}",
        {
            "subject_digest": subject_digest[:23],
            "statement_sha256": statement_sha,
            "envelope_sha256": hashlib.sha256(envelope_json.encode()).hexdigest(),
            "algo": algo,
            "replaced": existing is not None,
        },
    )
    return ProvenanceRecord(model, version, subject_digest, algo, kid, statement_sha, envelope)


def _same_or_raise(model: str, version: str, row: dict[str, Any], fingerprint: str) -> None:
    stored = _statement_of_row(row)
    if stored is not None and _fingerprint(stored) == fingerprint:
        return
    raise ProvenanceExists(
        f"{model}@{version} already has provenance recorded "
        f"(statement {row['statement_sha256'][:12]}); pass replace to overwrite"
    )


def _fingerprint(statement: dict[str, Any]) -> str:
    clone = json.loads(json.dumps(statement))
    meta = clone.get("predicate", {}).get("runDetails", {}).get("metadata", {})
    meta.pop("finishedOn", None)
    meta.pop("startedOn", None)
    return hashlib.sha256(canonical(clone)).hexdigest()


# ── storage (additive table `model_provenance`, ADR 0013) ─────────────────────────────────


def _store_row(
    model: str,
    version: str,
    subject_digest: str,
    statement_sha: str,
    algo: str,
    kid: str | None,
    envelope_json: str,
    builder: str,
    actor: str | None,
    *,
    mode: str = "insert",
) -> bool:
    """Write the row; True when this call wrote it.

    ``insert`` never touches an existing row (the read-then-write in ``record_provenance`` is not
    atomic, and a concurrent recorder must not be overwritten), ``upgrade`` overwrites only an
    *unsigned* row, ``replace`` overwrites unconditionally (the caller asked for it).
    """
    from examlops.data import get_db, init_db  # noqa: PLC0415

    on_conflict = {
        "insert": "DO NOTHING",
        "upgrade": _UPSERT + f" WHERE model_provenance.algo = '{UNSIGNED}'",
        "replace": _UPSERT,
    }[mode]
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO model_provenance
                   (model, version, subject_digest, statement_sha256, algo, key_id,
                    envelope_json, builder_id, recorded_by, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(model, version) """
            + on_conflict,
            (
                _norm_model(model),
                str(version),
                subject_digest,
                statement_sha,
                algo,
                kid,
                envelope_json,
                builder,
                actor,
            ),
        )
        return bool(cur.rowcount and cur.rowcount > 0)


_UPSERT = """DO UPDATE SET
                   subject_digest=excluded.subject_digest,
                   statement_sha256=excluded.statement_sha256, algo=excluded.algo,
                   key_id=excluded.key_id, envelope_json=excluded.envelope_json,
                   builder_id=excluded.builder_id, recorded_by=excluded.recorded_by,
                   recorded_at=CURRENT_TIMESTAMP"""


def _get_row(model: str, version: str) -> dict[str, Any] | None:
    from examlops.data import get_db, init_db  # noqa: PLC0415

    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM model_provenance WHERE model=? AND version=?",
            (_norm_model(model), str(version)),
        ).fetchone()
    return dict(row) if row else None


def _statement_of_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """The statement a stored envelope carries, **unverified** (for display and idempotency)."""
    try:
        env = json.loads(row["envelope_json"])
        if "sigstoreBundle" in env:
            payload = env["sigstoreBundle"]["dsseEnvelope"]["payload"]
        else:
            payload = env["payload"]
        decoded: dict[str, Any] = json.loads(base64.b64decode(payload))
        return decoded
    except Exception:  # noqa: BLE001 - a malformed row has no readable statement
        return None


def _record_from_row(row: dict[str, Any]) -> ProvenanceRecord:
    return ProvenanceRecord(
        row["model"],
        row["version"],
        row["subject_digest"],
        row["algo"],
        row["key_id"],
        row["statement_sha256"],
        json.loads(row["envelope_json"]),
    )


def get_provenance(model: str, version: str) -> dict[str, Any] | None:
    """The stored row plus its decoded (unverified) statement, or None."""
    row = _get_row(model, version)
    if row is None:
        return None
    out = dict(row)
    out["envelope"] = json.loads(out.pop("envelope_json"))
    out["statement"] = _statement_of_row(row)
    return out


# ── verification ──────────────────────────────────────────────────────────────────────────


def verify_provenance(
    model: str, version: str, *, expected_digest: str | None = None
) -> ProvenanceVerdict:
    """Verify the recorded provenance of ``model@version``. Fail-closed.

    Reasons: ``verified``, ``missing``, ``unsigned``, ``malformed: …``, ``untrusted-key: …``,
    ``untrusted-identity: …``, ``bad-signature``, ``unavailable: …``, ``subject-mismatch: …``.
    ``expected_digest`` (the digest the model signature covers, or one recomputed from the
    artifacts) must equal the statement's subject digest.
    """
    row = _get_row(model, version)
    if row is None:
        return ProvenanceVerdict(False, "missing: no provenance recorded")
    algo = row["algo"]
    if algo == UNSIGNED:
        return ProvenanceVerdict(False, "unsigned: provenance recorded without a signing key")
    try:
        envelope = json.loads(row["envelope_json"])
    except ValueError:
        return ProvenanceVerdict(False, "malformed: envelope is not JSON")
    payload: bytes | None
    if algo == SIGSTORE:
        from examlops.supplychain import keyless  # noqa: PLC0415

        verdict, payload = keyless.verify_statement(json.dumps(envelope.get("sigstoreBundle")))
        if not verdict.ok:
            return ProvenanceVerdict(False, verdict.reason)
    elif algo == ED25519_DSSE:
        try:
            if envelope["payloadType"] != PAYLOAD_TYPE:
                return ProvenanceVerdict(False, "malformed: unexpected payload type")
            payload = base64.b64decode(envelope["payload"])
            sigs = envelope["signatures"]
        except (KeyError, ValueError, TypeError):
            return ProvenanceVerdict(False, "malformed: not a DSSE envelope")
        from examlops import supplychain  # noqa: PLC0415

        reasons = [
            supplychain.ed25519_verify_bytes(
                pae(PAYLOAD_TYPE, payload), s.get("sig", ""), s.get("keyid", "")
            )
            for s in sigs
            if isinstance(s, dict)
        ]
        if "verified" not in reasons:
            return ProvenanceVerdict(False, reasons[0] if reasons else "unsigned: no signatures")
    else:
        return ProvenanceVerdict(False, f"malformed: unknown algorithm {algo!r}")
    try:
        statement = json.loads(payload or b"")
        subject = statement["subject"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return ProvenanceVerdict(False, "malformed: payload is not an in-toto statement")
    if statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != PREDICATE_TYPE:
        return ProvenanceVerdict(False, "malformed: not an SLSA v1 provenance statement")
    if subject.get("name") != subject_name(model, version):
        return ProvenanceVerdict(
            False, f"subject-mismatch: statement names {subject.get('name')!r}", statement
        )
    signed_digest = "sha256:" + str(subject.get("digest", {}).get("sha256", ""))
    if signed_digest != row["subject_digest"]:
        return ProvenanceVerdict(False, "subject-mismatch: row digest was rewritten", statement)
    if expected_digest is not None and signed_digest != expected_digest:
        return ProvenanceVerdict(
            False, "subject-mismatch: provenance covers different artifact bytes", statement
        )
    return ProvenanceVerdict(True, "verified", statement)
