"""The serving snapshot: what every serving replica needs to know, compiled once (ADR 0127, P4.2).

Before this, each Ray Serve replica rebuilt its view of the world on its own: every
``RAY_RELOAD_POLL_SECONDS`` it listed MLflow's registered models (one page — a registry past 100
models silently lost the rest) and asked for every alias of every model, then read the shadow and
traffic tables on a TTL. Replicas × models × aliases MLflow calls per minute, a view that could
differ between replicas, and nothing to say which view a replica was acting on.

Now one compiler, run by the control plane, produces a snapshot::

    {"schema": 1, "generation": 42, "digest": "sha256:…", "compiled_at": "…Z",
     "models":  {"jpcp": {"name": "jpcp", "aliases": {"Production": {"version": "7",
                           "run_id": "…", "source": "s3://…", "framework": "sklearn",
                           "signature": {"algo": "ed25519-v2", "digest": "sha256:…",
                                         "signature": "…", "cert": "<key id>"}}}}},
     "traffic": {"jpcp": {"model": "JPCP", "rules": {"Production": 90, "Canary": 10}}},
     "shadow":  {"jpcp": {"model": "JPCP", "alias": "Staging"}},
     "quotas":  {"tenants": {"acme": {"rpm": 120}}}}

* ``quotas`` are per-tenant request limits (requests per minute; ``0`` = unlimited for that tenant)
  the serving gateway enforces in place of its own default (ADR 0123 decision 3). A tenant with no
  entry keeps the gateway's ``EXAMLOPS_GATEWAY_TENANT_RPM``.
* ``input_schema`` (optional, inside an alias entry) is the input schema of that model version, from
  its MLflow signature (:mod:`examlops.serving_schema`). A replica checks requests against it
  without reading anything. It is inside ``models``, so the digest already covers it; a version
  with no signature has no key.
* ``generation`` is monotonic and only moves when the content (``digest``) does, so a replica can
  report exactly which configuration it is serving and lag is a subtraction.
* Snapshots are stored in ``serving_snapshots`` (the replica's pull path, and last-known-good for
  a replica that restarts while the control plane is down), mirrored to the NATS key-value bucket
  ``examlops-serving`` when the event backbone is NATS, and announced as
  ``serving.snapshot_published``.
* Keys are the lower-case serving model key; ``name`` keeps the registry's spelling.
* Each version carries its signature record (``null`` when unsigned, plan P4.10), so a replica
  verifies what it loads against the snapshot's copy — with the database unreachable too. The
  record is only data: it passes only if it verifies under a key in the replica's trust bundle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
KV_BUCKET = "examlops-serving"
KV_KEY = "snapshot"
TRIGGER_TOPICS = (
    "serving.traffic_changed",
    "serving.shadow_changed",
    "serving.quota_changed",
    "model.alias_changed",
)
# The sections the digest covers. ``quotas`` arrived after the first generations were published
# (ADR 0123 d3), so a reader hashes only the sections a snapshot actually carries — an older
# snapshot still verifies, and a newer one cannot lose a section without its digest failing.
CONTENT_KEYS = ("models", "traffic", "shadow", "quotas")
_KEEP = 50  # snapshot rows retained; older generations are pruned on publish

# MLflow model versions are immutable, so what we learn about (name, version) is cached for the
# life of the process: a recompile after one promotion costs one model-version lookup, not N.
_version_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
_version_cache_lock = threading.Lock()
# The input schema of a version, likewise immutable. Only a *definite* answer is cached — a schema,
# or "this version declares none" — never a failed read, which would otherwise pin "no schema" (and
# so drop the check from every replica) until the process restarts.
_schema_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}


def _mlflow_url() -> str:
    return os.getenv("MLFLOW_TRACKING_URI", "http://localhost:15000").rstrip("/")


def _registered_models(client: Any, base: str) -> list[dict[str, Any]]:
    """Every registered model, following ``next_page_token`` (the 100-model bug, by design)."""
    models: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {"max_results": 1000}
        if token:
            params["page_token"] = token
        r = client.get(f"{base}/api/2.0/mlflow/registered-models/search", params=params)
        r.raise_for_status()
        body = r.json()
        models.extend(body.get("registered_models", []))
        token = body.get("next_page_token")
        if not token:
            return models


def _version_facts(client: Any, base: str, name: str, version: str) -> dict[str, Any]:
    key = (base, name, version)
    with _version_cache_lock:
        cached = _version_cache.get(key)
    if cached is not None:
        return cached
    r = client.get(
        f"{base}/api/2.0/mlflow/model-versions/get", params={"name": name, "version": version}
    )
    r.raise_for_status()
    mv = r.json().get("model_version", {})
    tags = {t.get("key"): t.get("value") for t in mv.get("tags", []) or []}
    facts = {
        "version": str(version),
        "run_id": mv.get("run_id"),
        "source": mv.get("source"),
        "framework": (tags.get("framework") or "sklearn").lower(),
    }
    with _version_cache_lock:
        _version_cache[key] = facts
    return facts


def _previous_schemas() -> dict[tuple[str, str], dict[str, Any]]:
    """``(model key, version) -> input schema`` in the newest published snapshot (best effort)."""
    try:
        previous = latest() or {}
    except Exception:  # noqa: BLE001 - no previous snapshot is a fine answer
        return {}
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for key, entry in (previous.get("models") or {}).items():
        for facts in (entry.get("aliases") or {}).values():
            if isinstance(facts, dict) and facts.get("input_schema"):
                found[(key, str(facts.get("version")))] = facts["input_schema"]
    return found


def _version_schema(
    client: Any, base: str, name: str, facts: dict[str, Any], prior: dict[tuple, Any]
) -> dict[str, Any] | None:
    """The input schema of one model version, from the ``MLmodel`` signature MLflow serves.

    A version without a readable signature has no schema and is served unchecked. A read that
    *fails* (the artifact store is down) is not "no schema": the schema the previous snapshot
    carried for this same immutable version is kept, so an artifact-store blip cannot lift the
    check on every replica and then restore it a minute later as two spurious generations.
    """
    from examlops import serving_schema  # noqa: PLC0415

    if os.getenv("EXAMLOPS_SNAPSHOT_INPUT_SCHEMAS", "1").strip().lower() in ("0", "false", "off"):
        return None
    key = (base, name, facts["version"])
    with _version_cache_lock:
        if key in _schema_cache:
            return _schema_cache[key]
    where = serving_schema.mlmodel_location(facts.get("source"), facts.get("run_id"))
    schema: dict[str, Any] | None = None
    if where is not None:
        route, params = where
        try:
            r = client.get(base + route, params=params)
            if getattr(r, "status_code", 200) == 404:
                pass  # the artifact is definitely absent: the version has no schema
            else:
                r.raise_for_status()
                schema = serving_schema.parse_mlmodel(getattr(r, "text", "") or "")
        except Exception as exc:  # noqa: BLE001 - transient: keep what the last snapshot knew
            logger.warning("Input schema of %s v%s unreadable: %s", name, facts["version"], exc)
            return prior.get((name.strip().lower(), str(facts["version"])))
    with _version_cache_lock:
        _schema_cache[key] = schema
    return schema


def _compile_models(client: Any, base: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    prior: dict[tuple, Any] | None = None
    for rm in _registered_models(client, base):
        name = rm.get("name")
        if not name:
            continue
        aliases: dict[str, Any] = {}
        for a in rm.get("aliases", []) or []:
            alias, version = a.get("alias"), a.get("version")
            if alias and version is not None:
                facts = _version_facts(client, base, name, str(version))
                if prior is None:
                    prior = _previous_schemas()
                schema = _version_schema(client, base, name, facts, prior)
                # No schema => no key, so a model without one hashes exactly as it always did.
                aliases[alias] = {**facts, "input_schema": schema} if schema else facts
        out[name.strip().lower()] = {"name": name, "aliases": dict(sorted(aliases.items()))}
    return dict(sorted(out.items()))


def _compile_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """Traffic splits and enabled shadow targets from the platform database."""
    from examlops.data import get_db, init_db  # noqa: PLC0415

    init_db()
    traffic: dict[str, Any] = {}
    shadow: dict[str, Any] = {}
    with get_db() as conn:
        for row in conn.execute("SELECT model, rules FROM traffic_rules ORDER BY model"):
            try:
                rules = json.loads(row["rules"]) if row["rules"] else None
            except (TypeError, ValueError):
                rules = None
            if rules:
                traffic[str(row["model"]).lower()] = {"model": row["model"], "rules": rules}
        for row in conn.execute(
            "SELECT model, shadow_alias FROM shadow_config WHERE enabled=1 ORDER BY model"
        ):
            shadow[str(row["model"]).lower()] = {
                "model": row["model"],
                "alias": row["shadow_alias"],
            }
    return traffic, shadow


def _compile_quotas() -> dict[str, Any]:
    """Per-tenant request quotas (``serving_quotas``), keyed by tenant."""
    from examlops.data import serving_quotas  # noqa: PLC0415

    return {"tenants": {q["tenant"]: {"rpm": int(q["rpm"])} for q in serving_quotas.list_quotas()}}


def _attach_signatures(models: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Copy each version's ``model_signatures`` row into its facts, in one read."""
    from examlops.data import get_db, init_db  # noqa: PLC0415

    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT model, version, algo, digest, signature, cert FROM model_signatures"
        ).fetchall()
    signed = {
        (str(r["model"]).lower(), str(r["version"])): {
            "algo": r["algo"],
            "digest": r["digest"],
            "signature": r["signature"],
            "cert": r["cert"],
        }
        for r in rows
    }
    out: dict[str, dict[str, Any]] = {}
    for key, entry in models.items():
        aliases = {
            alias: {**facts, "signature": signed.get((key, str(facts["version"])))}
            for alias, facts in entry["aliases"].items()
        }
        out[key] = {**entry, "aliases": aliases}
    return out


def digest_of(content: dict[str, Any]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def compile_snapshot(*, client: Any = None, mlflow_url: str | None = None) -> dict[str, Any]:
    """Build a snapshot (without a generation — :func:`publish` assigns one).

    Raises when MLflow cannot be read: a partial snapshot would tell every replica that the
    models it could not see had lost their aliases, and replicas unload what the snapshot omits.
    """
    base = (mlflow_url or _mlflow_url()).rstrip("/")
    own = client is None
    if own:
        import httpx  # noqa: PLC0415

        from examlops.service_auth import mlflow_headers  # noqa: PLC0415

        client = httpx.Client(
            timeout=float(os.getenv("EXAMLOPS_SNAPSHOT_MLFLOW_TIMEOUT", "10")),
            headers=mlflow_headers(),  # MLflow basic-auth / token when enabled (plan P3.6)
        )
    try:
        models = _compile_models(client, base)
    finally:
        if own:
            client.close()
    models = _attach_signatures(models)
    traffic, shadow = _compile_config()
    content = {
        "models": models,
        "traffic": traffic,
        "shadow": shadow,
        "quotas": _compile_quotas(),
    }
    return {
        "schema": SCHEMA_VERSION,
        "digest": digest_of(content),
        "compiled_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        **content,
    }


def latest_generation() -> int | None:
    """The newest published generation, without reading the body (a replica's cheap poll)."""
    from examlops.data import get_db, init_db  # noqa: PLC0415

    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT MAX(generation) FROM serving_snapshots").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def latest() -> dict[str, Any] | None:
    """The newest published snapshot, or ``None`` when none has been published."""
    from examlops.data import get_db, init_db  # noqa: PLC0415

    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT generation, body FROM serving_snapshots ORDER BY generation DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    snapshot = json.loads(row["body"])
    snapshot["generation"] = int(row["generation"])
    return snapshot


def publish(snapshot: dict[str, Any], *, actor: str = "control-plane") -> tuple[int, bool]:
    """Store ``snapshot`` as the next generation unless its content is unchanged.

    Returns ``(generation, published)``. The row, its ``serving.snapshot_published`` event and
    the pruning of old generations commit together. Mirroring to NATS KV happens after commit
    and is best-effort: the database row is the source of truth and replicas fall back to it.
    """
    from examlops.data import _immediate_write, init_db, write_retry  # noqa: PLC0415
    from examlops.data.events import enqueue_event  # noqa: PLC0415

    init_db()
    body = {k: v for k, v in snapshot.items() if k != "generation"}

    def _store() -> tuple[int, bool]:
        with _immediate_write("serving-snapshot") as conn:
            row = conn.execute(
                "SELECT generation, digest FROM serving_snapshots ORDER BY generation DESC LIMIT 1"
            ).fetchone()
            if row is not None and row["digest"] == body["digest"]:
                return int(row["generation"]), False
            cur = conn.execute(
                "INSERT INTO serving_snapshots (digest, body) VALUES (?, ?)",
                (body["digest"], json.dumps(body, sort_keys=True, default=str)),
            )
            generation = int(cur.lastrowid or 0)
            conn.execute(
                "DELETE FROM serving_snapshots WHERE generation <= ?", (generation - _KEEP,)
            )
            enqueue_event(
                "serving.snapshot_published",
                {
                    "generation": generation,
                    "digest": body["digest"],
                    "models": len(body.get("models", {})),
                },
                conn=conn,
                actor=actor,
            )
            return generation, True

    generation, published = write_retry(_store)
    if published:
        _mirror_to_kv({**body, "generation": generation})
    return generation, published


def _mirror_to_kv(snapshot: dict[str, Any]) -> None:
    if os.getenv("EXAMLOPS_EVENT_PUBLISHER", "log").strip().lower() != "nats":
        return
    try:
        from examlops.events import nats_backend  # noqa: PLC0415

        nats_backend.shared().kv_put(
            KV_BUCKET, KV_KEY, json.dumps(snapshot, sort_keys=True, default=str).encode()
        )
    except Exception as exc:  # noqa: BLE001 - replicas fall back to the database row
        logger.warning(
            "Serving snapshot %s not mirrored to NATS KV: %s", snapshot["generation"], exc
        )


def trigger_watermark() -> int:
    """The newest outbox id among the topics that change what serving should do."""
    from examlops.data import get_db, init_db  # noqa: PLC0415

    init_db()
    placeholders = ",".join("?" * len(TRIGGER_TOPICS))
    with get_db() as conn:
        row = conn.execute(
            f"SELECT MAX(id) FROM event_outbox WHERE topic IN ({placeholders})", TRIGGER_TOPICS
        ).fetchone()
    return int(row[0] or 0) if row else 0


def compile_and_publish(*, actor: str = "control-plane") -> tuple[int, bool]:
    """One projector step: compile from the sources of truth, publish if anything changed."""
    return publish(compile_snapshot(), actor=actor)
