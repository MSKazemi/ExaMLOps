"""The agent snapshot: everything the agent runtime needs, compiled on the control plane (ADR 0144 d5).

The runtime is serving plane and statically stable: on the request path it makes **no** call to
the control plane, MLflow, Prefect or ``platform.db``. It learns which agent versions exist,
where their aliases point, the canary share, the tool grants, what ``follow`` bindings resolve
to and the per-tenant quotas from this document - compiled here, handed to the runtime as a file
(or pushed), and kept by the runtime in its own state store as last-known-good, so a runtime
that restarts while the control plane is down still serves the last configuration it saw.

::

    {"schema": 1, "generation": 1758800000123, "digest": "sha256:...", "compiled_at": "...Z",
     "agents":   {"jobdoc": {"aliases": {"Production": "av-sha256:..."}, "canary_percent": 10,
                             "migrations": {"av-old": {"to": "av-new", "outcome": "compatible"}},
                             "retired": {"av-bad": "quarantine"}}},
     "versions": {"av-sha256:...": {<normalised manifest>}},
     "grants":   {"<subject>": {"<tool>": {<grant doc>}}},
     "models":   {"qwen3-32b": {"Production": "7"}},
     "reeval_pins": {"av-sha256:...": {"qwen3-32b@Production": "6"}},
     "quotas":   {"default": {"max_sessions": 100}, "tenants": {"acme": {"max_sessions": 5,
                  "sandbox_isolation": "gvisor"}}}}

* ``migrations`` - from the state-compatibility verdict recorded on the latest Production move
  (ADR 0146 d5): what threads still on the old version do.
* ``retired`` - from the latest rollback's in-flight policy (ADR 0146 d4).
* ``reeval_pins`` - blocking follow-binding re-evaluations not yet passed (ADR 0146 d2).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "compile_agent_snapshot",
    "digest_of",
    "load_snapshot_file",
    "validate_snapshot",
    "write_snapshot",
]

SCHEMA_VERSION = 1
_SECTIONS = ("agents", "versions", "grants", "models", "reeval_pins", "quotas")
_RECENT_PER_AGENT = 50


def digest_of(doc: dict[str, Any]) -> str:
    """Content digest over every section AND the generation and schema.

    The generation is what the runtime orders snapshots by (an older one is refused), so it is
    sealed too: a corrupted generation that escaped the digest could pin a runtime to one
    snapshot forever, every real update then looking "older".
    """
    body: dict[str, Any] = {k: doc.get(k, {}) for k in _SECTIONS}
    body["generation"] = doc.get("generation")
    body["schema"] = doc.get("schema")
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_snapshot(doc: Any) -> list[str]:
    """Problems with a snapshot document (empty = usable)."""
    if not isinstance(doc, dict):
        return ["snapshot: must be a JSON object"]
    out: list[str] = []
    if doc.get("schema") != SCHEMA_VERSION:
        out.append(f"schema: must be {SCHEMA_VERSION}")
    gen = doc.get("generation")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 0:
        out.append("generation: must be a non-negative integer")
    for k in _SECTIONS:
        if not isinstance(doc.get(k, {}), dict):
            out.append(f"{k}: must be an object")
    if not out and doc.get("digest") != digest_of(doc):
        out.append("digest: does not match the content (corrupt or edited snapshot)")
    return out


def _quotas_from_env() -> dict[str, Any]:
    raw = os.getenv("EXAMLOPS_AGENT_QUOTAS", "").strip()
    default = {"max_sessions": int(os.getenv("EXAMLOPS_AGENT_MAX_SESSIONS", "100") or 100)}
    if not raw:
        return {"default": default, "tenants": {}}
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("EXAMLOPS_AGENT_QUOTAS must be a JSON object")
    return {"default": {**default, **doc.get("default", {})}, "tenants": doc.get("tenants", {})}


def _models_from_serving_snapshot() -> dict[str, dict[str, str]]:
    """``{model_key: {alias: version}}`` from the latest serving snapshot (ADR 0127), if any."""
    try:
        from examlops import serving_snapshot

        snap = serving_snapshot.latest() or {}
    except Exception:  # noqa: BLE001 - no serving snapshot: follow bindings resolve from `models=`
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, m in (snap.get("models") or {}).items():
        aliases = {a: str(v.get("version")) for a, v in (m.get("aliases") or {}).items()}
        if aliases:
            out[str(key).lower()] = aliases
    return out


def compile_agent_snapshot(
    *,
    models: dict[str, dict[str, str]] | None = None,
    quotas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile the agent snapshot from ``platform.db`` (control-plane side; never on a request).

    ``models`` overrides/extends the follow-binding resolution table (``{key: {alias: version}}``);
    by default it comes from the latest serving snapshot.
    """
    from examlops.agent_versions.reeval import pinned_overrides
    from examlops.data import agent_versions as store
    from examlops.tool_broker.service import list_grants

    agents: dict[str, dict[str, Any]] = {}
    wanted: set[str] = set()
    for a in store.list_aliases():
        entry = agents.setdefault(
            a["agent"], {"aliases": {}, "canary_percent": 0.0, "migrations": {}, "retired": {}}
        )
        entry["aliases"][a["alias"]] = a["version_id"]
        wanted.add(a["version_id"])
    for r in store.list_rollouts():
        if r["agent"] in agents:
            agents[r["agent"]]["canary_percent"] = float(r["canary_percent"])
    for name, entry in agents.items():
        for mv in store.latest_moves(name):
            ev = json.loads(mv["evidence_json"]) if mv.get("evidence_json") else {}
            if mv["action"] == "set" and mv["alias"] == "Production" and mv["prev_version"]:
                sc = ev.get("state_compat") or {}
                if sc:
                    outcome = (
                        "compatible" if sc.get("outcome") == "compatible" else sc.get("strategy")
                    )
                    if outcome:
                        entry["migrations"][mv["prev_version"]] = {
                            "to": mv["version_id"],
                            "outcome": outcome,
                        }
                        wanted.add(mv["prev_version"])
            if mv["action"] == "rollback" and mv["prev_version"]:
                policy = ev.get("in_flight") or "continue"
                entry["retired"][mv["prev_version"]] = policy
                wanted.add(mv["prev_version"])
        for v in store.list_versions(name, limit=_RECENT_PER_AGENT):
            wanted.add(v["version_id"])
    versions: dict[str, Any] = {}
    for vid in sorted(wanted):
        row = store.get_version(vid)
        if row is not None:
            versions[vid] = row["manifest"]
    subjects = set(versions) | set(agents)
    grants: dict[str, dict[str, Any]] = {}
    for g in list_grants():
        if g["subject"] in subjects:
            gdoc = {k: v for k, v in g.items() if k not in ("subject", "tool")}
            grants.setdefault(g["subject"], {})[g["tool"]] = gdoc
    resolved = _models_from_serving_snapshot()
    for key, aliases in (models or {}).items():
        resolved.setdefault(key.lower(), {}).update({a: str(v) for a, v in aliases.items()})
    pins = {vid: p for vid, p in pinned_overrides().items() if vid in versions}
    doc: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generation": int(time.time() * 1000),
        "compiled_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "agents": agents,
        "versions": versions,
        "grants": grants,
        "models": resolved,
        "reeval_pins": pins,
        "quotas": quotas if quotas is not None else _quotas_from_env(),
    }
    doc["digest"] = digest_of(doc)
    return doc


def write_snapshot(doc: dict[str, Any], path: str | Path) -> Path:
    """Write atomically (temp file + rename): a runtime never reads a half-written snapshot."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agent-snapshot-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True, indent=1)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def load_snapshot_file(path: str | Path) -> dict[str, Any]:
    """Read and validate a snapshot file. ``ValueError`` names every problem."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = validate_snapshot(doc)
    if problems:
        raise ValueError("; ".join(problems))
    return doc
