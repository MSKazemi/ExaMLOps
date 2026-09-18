"""Stream bindings — YAML/pack loading + tenancy-checked catalog writes.

ADR 0130/0131, Plan 2 batch S1 task A6 (ruling R11 / E15). Three entry points:

* :func:`yaml_streams` — parse ``inference.streams`` (plus the legacy ``seanerbus_uuid`` key,
  gated by ``EXAMLOPS_DATAPLANE_LEGACY_SEANERBUS_UUID``) out of one already-loaded model YAML dict
  into a list of :class:`StreamBinding`. Pure: no I/O, no catalog write, no tenancy check.
* :func:`sync_pack_streams` — scans the active pack's model YAMLs
  (:func:`examlops.usecase.models_dir`) and upserts every declared stream into the catalog as
  ``origin="pack"``. Idempotent: re-running it against an unchanged pack changes nothing but
  ``updated_at``. A pack stream whose YAML entry has since disappeared is **disabled** (never
  deleted), and never overwrites a row a human/API call owns (``origin="api"``) — see the function
  docstring for the exact ownership and error-isolation rules (fix round 1, findings 1/2/5).
* :func:`define_stream` — the API/CLI write path: name-validated, tenancy-checked (ruling R11),
  canonical-model-resolved, then ``upsert_stream(..., origin="api")`` + an audit event. Refuses to
  touch a pack-owned row (fix round 1, finding 1).

Tenancy — ruling R11: a stream in project ``P`` may bind model ``M`` only if ``P`` is one of the
projects ``M`` belongs to; a stream in the unscoped/default project (stored as ``""``, displayed
``_global`` — the same convention :mod:`examlops.dataplane.store` and
``examlops.dataplane.service.auth`` already use for ``project or "_global"``) may only bind a model
that has **no** project membership at all. ``examlops.serving_gateway._project_of`` (untracked,
owned by another session working on the bridge/gateway) does the equivalent single-project,
case-insensitive lookup; this module duplicates that query — rather than importing the untracked
module, which Plan 2 batch S1 must not do (HEAD-only imports) — and widens it to the *set* of a
model's projects, since ``project_resources`` has no uniqueness on ``ref`` alone.

**Fix round 1 (review findings, all binding controller rulings):**

1. *Origin ownership.* ``define_stream`` refuses to create or overwrite a ``origin="pack"`` row;
   ``sync_pack_streams`` never overwrites an ``origin="api"`` row (records it under
   ``report["conflicts"]`` instead). Both are enforced twice: a pre-check here (a clear error
   message / a log line) and atomically inside
   :func:`examlops.data.dataplane.upsert_stream` itself, which only updates a conflicting row when
   the existing and incoming ``origin`` match — closing the read-then-write race between two
   processes.
2. *Safe removal sweep.* The disable-sweep only runs when the whole sync loaded with zero
   parse/validation errors **and** the pack directory exists and holds at least one model YAML.
   A sweep-disable is tagged ``state_reason="removed_from_pack"`` and audited
   (``dataplane_stream_disabled``); a stream re-appearing in the pack is re-enabled **only** if
   that is why it was disabled (audited as ``dataplane_stream_enabled``,
   ``reason="readded_to_pack"``) — a human's pause/disable (any other reason, or none) is left
   alone.
3. *Unicode-safe tenancy.* ``name``/``model``/a non-empty ``project`` are validated against the
   committed ASCII allowlist (:func:`examlops.dataplane.safety.validate_name`) before any lookup,
   in both ``define_stream`` and the pack YAML parsers — closing a gap where SQLite's ASCII-only
   ``lower()`` and Ray Serve's Python ``str.lower()`` disagree on a non-ASCII spelling (e.g. the
   Kelvin sign U+212A), which could let a scoped model's stream slip past R11 as "unscoped".
4. *Canonical model spelling.* ``define_stream`` resolves ``model`` case-insensitively against the
   active pack's model YAMLs and stores the pack's own canonical spelling (raises
   ``SpecError("unknown model <m>")`` otherwise) — telemetry/drift use ``binding.model`` verbatim
   with no case folding of their own, so an uncanonicalised spelling would silently split a
   model's drift history across two names.
5. *Per-entry robustness.* Each pack YAML entry is parsed and tenancy-checked in its own
   try/except (``SpecError``/``ValueError``/``TypeError``/``AttributeError``); a failing entry is
   recorded under ``report["errors"]`` (file, entry index, message — never the raw parser
   exception text, which could embed payload content) and the rest of the file/pack keeps going.
   Any such error suppresses the removal sweep for that run (see finding 2).

**Fix round 2 (re-review findings I5/N1, controller rulings, binding):**

5b. *I5, closed.* Round 1's per-entry isolation still let four shapes of malformed-but-parseable
    content escape: a non-string ``project``, a YAML date inside ``options`` (fails
    ``json.dumps``), and a non-string ``address``/``connection`` (raised deep inside the SQLite
    driver, *outside* the per-entry ``try``). ``_binding_from_entry`` now type-checks every field
    up front (``name``/``connector``/``alias``/``address`` must be ``str``; ``connection`` must be
    ``str``/``None``; ``options`` must be a JSON-serialisable dict with no ``default=`` bail-out;
    ``limits`` must be a dict of ``int``/``None`` values) and ``normalize_project`` rejects a
    non-``str`` outright. On top of that, the per-entry (and per-file) loop in
    :func:`sync_pack_streams` now wraps its *entire* body — construction, tenancy check, **and the
    upsert** — in one ``except Exception`` as a last-resort net: never ``str(exc)`` for the
    unexpected-exception branch (only ``type(exc).__name__`` + the file/entry position), since an
    exception this module did not itself raise (a DB driver error, say) may embed the entry's own
    values.
6. *N1, closed.* Round 1's re-enable/sweep pair silently promoted a **paused** stream straight to
   **enabled** across a removal-and-re-add, losing the human's pause. The sweep now folds the
   prior state into the reason it writes (``"removed_from_pack"`` for a row that was ``enabled``,
   ``"removed_from_pack:paused"`` for one that was ``paused``) and restores exactly that state on
   re-add, clearing the reason. A row already ``disabled`` (any reason, including ``None``) is
   still left completely untouched by the sweep — no state change, no reason overwrite, no audit —
   exactly as round 1 already did.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from examlops.data import get_db, init_db
from examlops.data.audit import write_audit_event
from examlops.data.dataplane import (
    STREAM_STATES,
    get_stream,
    list_streams,
    set_stream_state,
    upsert_stream,
)
from examlops.dataplane.safety import validate_name
from examlops.dataplane.streams.types import (
    GLOBAL_PROJECT,
    StreamBinding,
    StreamLimits,
    display_project,
    normalize_project,
)
from examlops.dataplane.types import SpecError
from examlops.usecase import models_dir

log = logging.getLogger(__name__)

__all__ = [
    "GLOBAL_PROJECT",
    "define_stream",
    "get_binding",
    "is_sweep_reason",
    "list_bindings",
    "sync_pack_streams",
    "yaml_streams",
]

#: ``GLOBAL_PROJECT``/``normalize_project``/``display_project`` now live in
#: :mod:`examlops.dataplane.streams.types` — the one module every stream module can import without
#: pulling in ``yaml`` or the datastore (review M3). They are re-exported here, where they were
#: first published, so existing callers keep working.

#: Legacy shim gate (E15) — the pre-Plan-2 SeanerBUS bridge derived one binding per model straight
#: from ``seanerbus_uuid``. Default OFF: a model with real ``inference.streams`` entries should not
#: also get a shadow legacy binding unless an operator opts in during the cutover window.
_LEGACY_ENV = "EXAMLOPS_DATAPLANE_LEGACY_SEANERBUS_UUID"

#: Sweep-disable/re-enable reason (fix round 1, finding 2). Never applied to a human disable/pause,
#: so the sweep can tell "I did this" apart from "an operator did this" on the next run. Fix round
#: 2 (N1) folds the prior state into the reason for a non-"enabled" row, so a pause survives a
#: removal-and-re-add instead of being silently promoted to "enabled" — see
#: ``_sweep_disable_reason``/``_sweep_restore_state``.
_SWEEP_REMOVED_REASON = "removed_from_pack"


def _sweep_disable_reason(prior_state: str) -> str:
    """The ``state_reason`` the sweep writes when disabling a row that was ``prior_state``."""
    return (
        _SWEEP_REMOVED_REASON
        if prior_state == "enabled"
        else f"{_SWEEP_REMOVED_REASON}:{prior_state}"
    )


def _sweep_restore_state(reason: str | None) -> str | None:
    """The state to restore a re-added row to, or ``None`` if *reason* isn't one the sweep itself
    wrote (a human's disable/pause reason, or no reason at all, must never be "restored")."""
    if reason == _SWEEP_REMOVED_REASON:
        return "enabled"
    if isinstance(reason, str) and reason.startswith(f"{_SWEEP_REMOVED_REASON}:"):
        suffix = reason[len(f"{_SWEEP_REMOVED_REASON}:") :]
        return suffix if suffix in STREAM_STATES else None
    return None


def is_sweep_reason(reason: str | None) -> bool:
    """Whether ``reason`` is one the pack-removal sweep would itself write — :data:`
    _SWEEP_REMOVED_REASON` alone, or with ``:<prior_state>`` (see :func:`_sweep_disable_reason`).

    The single source of truth for "a reason reserved for the sweep": a caller setting a
    stream's state through the API (``examlops.dataplane.service.app``'s ``POST
    /streams/{name}/state``) must refuse one of these, or a later sweep run could mistake a
    human's own reason for its own tag and silently "restore" a state the human specifically
    walked away from (see :func:`_sweep_restore_state`).
    """
    if reason == _SWEEP_REMOVED_REASON:
        return True
    return isinstance(reason, str) and reason.startswith(f"{_SWEEP_REMOVED_REASON}:")


_LIMITS_FIELDS = {f.name for f in dataclasses.fields(StreamLimits)}


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _limits_from_dict(raw: dict[str, Any] | None) -> StreamLimits:
    """Build a StreamLimits from a plain dict, ignoring unknown keys (forward-compatible)."""
    return StreamLimits(**{k: v for k, v in (raw or {}).items() if k in _LIMITS_FIELDS})


def _row_to_binding(row: dict[str, Any]) -> StreamBinding:
    return StreamBinding(
        project=row["project"],
        name=row["name"],
        connector=row["connector"],
        model=row["model"],
        alias=row["alias"],
        address=row["address"],
        connection=row["connection"],
        options=dict(row.get("options") or {}),
        limits=_limits_from_dict(row.get("limits")),
        state=row["state"],
        origin=row["origin"],
    )


def _upsert_binding(
    binding: StreamBinding, *, actor: str | None, origin: str | None = None
) -> bool:
    """Write *binding* via the catalog. Returns ``False`` when refused by the origin guard."""
    return upsert_stream(
        binding.project,
        binding.name,
        connector=binding.connector,
        model=binding.model,
        alias=binding.alias,
        address=binding.address,
        connection=binding.connection,
        options=dict(binding.options),
        limits=dataclasses.asdict(binding.limits),
        state=binding.state,
        origin=origin if origin is not None else binding.origin,
        actor=actor,
    )


def get_binding(name: str, project: str = GLOBAL_PROJECT) -> StreamBinding | None:
    """Fetch one stream binding, or ``None`` if it does not exist."""
    row = get_stream(name, normalize_project(project))
    return _row_to_binding(row) if row else None


def list_bindings(project: str | None = None) -> list[StreamBinding]:
    """List stream bindings, optionally scoped to one project."""
    scoped = None if project is None else normalize_project(project)
    return [_row_to_binding(r) for r in list_streams(scoped)]


# ── tenancy (ruling R11) ─────────────────────────────────────────────────────


def _projects_of_model(model: str) -> set[str]:
    """Every project *model* belongs to, matched case-insensitively (empty set == unscoped).

    Operators assign the registry spelling (``JPCP``); a stream definition may address the MLflow
    name (``jpcp``) — an exact match would find neither and every model would look unscoped, open
    to every project. Case-fold both sides, same as ``serving_gateway._project_of``. Safe against
    a Unicode case-fold mismatch (fix round 1, finding 3) only because every caller validates
    *model* as ASCII first — this function does not re-validate.
    """
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT project FROM project_resources WHERE kind='model' AND lower(ref)=lower(?) "
            "UNION SELECT project FROM project_models WHERE lower(model)=lower(?)",
            (model, model),
        ).fetchall()
    return {str(r["project"]) for r in rows}


def _check_tenancy(project: str, model: str) -> None:
    """Ruling R11. Raises SpecError without naming which other project (if any) owns the model."""
    projects = _projects_of_model(model)
    label = display_project(project)
    if project == GLOBAL_PROJECT:
        if projects:
            raise SpecError(f"model {model!r} is not a model of project {label!r}")
        return
    if project not in projects:
        raise SpecError(f"model {model!r} is not a model of project {label!r}")


# ── canonical model spelling (fix round 1, finding 4) ───────────────────────


def _canonical_model_names() -> dict[str, str]:
    """``{model.lower(): model}`` for every model the active pack declares.

    Best-effort per file — reuses the same directory (:func:`examlops.usecase.models_dir`) and
    loading approach (``yaml.safe_load`` + a bare ``name:`` read) :func:`sync_pack_streams` already
    uses, rather than the stricter :func:`pipelines.model_loader.load_model_yaml` (which requires
    ``config_class``/``task_type`` and would let one malformed sibling file block resolving every
    other model in the pack — and importing ``pipelines`` from the platform package would invert
    the platform/pipelines dependency direction, ADR 0094's layering). A file that fails to parse
    or has no usable ``name`` is skipped, never raised.
    """
    names: dict[str, str] = {}
    md = models_dir()
    if not md.is_dir():
        return names
    for path in sorted(md.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except Exception:  # noqa: BLE001 - a malformed sibling file must not block resolution
            continue
        if isinstance(raw, dict) and raw.get("name"):
            name = str(raw["name"])
            names.setdefault(name.strip().lower(), name)
    return names


def _resolve_canonical_model(model: str) -> str:
    """The pack's own spelling of *model* (case-insensitive lookup), or ``SpecError``."""
    canonical = _canonical_model_names().get(model.strip().lower())
    if canonical is None:
        raise SpecError(f"unknown model {model!r}")
    return canonical


# ── YAML parsing (pure) ──────────────────────────────────────────────────────


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise SpecError(f"{what} must be a string, got {type(value).__name__}")
    return value


def _binding_from_entry(
    entry: Any, *, project: str, model: str, origin: str = "pack"
) -> StreamBinding:
    """Build one :class:`StreamBinding` from one raw ``inference.streams`` entry.

    ``origin`` defaults to ``"pack"``, because a model YAML's ``inference.streams`` entry *is*
    pack content — this used to hard-code ``"api"``, so the one public pure parser
    (:func:`yaml_streams`) returned the wrong origin for exactly the bindings it exists to parse
    (review M12). The write path (:func:`_sync_one_binding`) passes ``origin="pack"`` to
    ``upsert_stream`` independently, so nothing stored ever changed; the returned value did.

    Type-checks every field up front (fix round 2, I5) — a pack YAML entry is untrusted input, and
    a YAML scalar can be anything the YAML grammar allows (an unquoted ``2024-01-01`` parses as a
    ``datetime.date``, a mapping is a valid value for any key, ...). Every failure raises
    ``SpecError`` with a message that names the field and the field's *type*, never the field's
    *value* — the caller (:func:`sync_pack_streams`) records this message verbatim, and a raw
    value could be a secret-bearing connection string or address.
    """
    if not isinstance(entry, dict):
        raise SpecError(f"inference.streams entry for model {model!r} must be a mapping")
    name = entry.get("name")
    connector = entry.get("connector")
    if not name or not connector:
        raise SpecError(f"inference.streams entry for model {model!r} needs 'name' and 'connector'")
    name = _require_str(name, "inference.streams entry 'name'")
    connector = _require_str(connector, f"inference.streams entry {name!r} 'connector'")
    validate_name(name, "stream name")

    alias = entry.get("alias") or "Production"
    alias = _require_str(alias, f"inference.streams entry {name!r} 'alias'")
    address = entry.get("address") or ""
    address = _require_str(address, f"inference.streams entry {name!r} 'address'")
    connection = entry.get("connection")
    if connection is not None:
        connection = _require_str(connection, f"inference.streams entry {name!r} 'connection'")

    options = entry.get("options") or {}
    if not isinstance(options, dict):
        raise SpecError(f"inference.streams entry {name!r} 'options' must be a mapping")
    try:
        # No `default=`: a value json can't natively encode (e.g. the datetime.date a bare YAML
        # date parses to) must be rejected here, not silently coerced and not left to blow up
        # deep inside examlops.data.dataplane.upsert_stream's own json.dumps call.
        json.dumps(options)
    except TypeError as exc:
        raise SpecError(
            f"inference.streams entry {name!r} 'options' is not JSON-serialisable"
        ) from exc

    raw_limits = entry.get("limits")
    if raw_limits is not None:
        if not isinstance(raw_limits, dict):
            raise SpecError(f"inference.streams entry {name!r} 'limits' must be a mapping")
        for key, val in raw_limits.items():
            if val is not None and not isinstance(val, int):
                raise SpecError(
                    f"inference.streams entry {name!r} limit {key!r} must be an int or null, "
                    f"got {type(val).__name__}"
                )

    state = entry.get("state") or "enabled"
    if state not in STREAM_STATES:
        raise SpecError(
            f"inference.streams entry {name!r} for model {model!r} has invalid state {state!r}"
        )
    return StreamBinding(
        project=project,
        name=name,
        connector=connector,
        model=model,
        alias=alias,
        address=address,
        connection=connection,
        options=dict(options),
        limits=_limits_from_dict(raw_limits),
        state=state,
        origin=origin,
    )


def _legacy_binding(
    model_yaml: dict[str, Any], *, project: str, model: str, origin: str = "pack"
) -> StreamBinding | None:
    """The legacy ``seanerbus_uuid`` shim binding, or ``None`` if the gate is off / no UUID.

    ``model`` is trusted to already be ASCII-validated (every caller validates it before this is
    reached), so the derived name ``f"{model}-seanerbus"`` needs no separate validation.
    """
    if not _truthy(os.getenv(_LEGACY_ENV)):
        return None
    uuid = model_yaml.get("seanerbus_uuid")
    if not uuid:
        return None
    return StreamBinding(
        project=project,
        name=f"{model}-seanerbus",
        connector="seanerbus",
        model=model,
        alias="Production",
        address=str(uuid),
        connection=None,
        options={},
        limits=StreamLimits(),
        state="enabled",
        origin=origin,  # pack content, like every other YAML-declared stream (M12)
    )


def yaml_streams(model_yaml: dict[str, Any], *, project: str) -> list[StreamBinding]:
    """Parse ``inference.streams`` (+ the legacy ``seanerbus_uuid`` shim) out of one already-loaded
    model YAML dict. Pure — no I/O, no catalog write, no tenancy check (that is
    :func:`define_stream`/:func:`sync_pack_streams`'s job). Raises on the first invalid entry —
    :func:`sync_pack_streams` does **not** call this in bulk; it re-implements the same per-entry
    construction with its own try/except per entry (fix round 1, finding 5), so one bad entry in
    a pack file never costs the rest.

    The binding's ``model`` is the YAML's canonical ``name`` field, unchanged (no case folding —
    matches the bridge's own model spelling, drift's ``model`` column). ``project`` accepts either
    ``""`` or ``"_global"`` for the unscoped default; both normalize to ``""``. ``name``/``model``/
    a non-empty ``project`` are ASCII-validated (fix round 1, finding 3) before anything else.
    """
    model = model_yaml.get("name")
    if not model:
        raise SpecError("model YAML is missing 'name'")
    model = str(model)
    validate_name(model, "model")
    normalized_project = normalize_project(project)
    if normalized_project:
        validate_name(normalized_project, "project")
    inference = model_yaml.get("inference")
    if inference is None:
        inference = {}
    if not isinstance(inference, dict):
        raise SpecError(f"model {model!r}: 'inference' must be a mapping")
    raw_streams = inference.get("streams") or []
    if not isinstance(raw_streams, list):
        raise SpecError(f"model {model!r}: 'inference.streams' must be a list")
    bindings = [
        _binding_from_entry(entry, project=normalized_project, model=model) for entry in raw_streams
    ]
    legacy = _legacy_binding(model_yaml, project=normalized_project, model=model)
    if legacy is not None and not any(b.connector == "seanerbus" for b in bindings):
        bindings.append(legacy)
    return bindings


# ── pack sync (origin="pack") ────────────────────────────────────────────────


def _sync_one_binding(
    binding: StreamBinding,
    *,
    actor: str | None,
    report: dict[str, list[Any]],
    seen: set[tuple[str, str]],
) -> None:
    """Upsert one already-validated, already-tenancy-checked pack binding. Handles the
    origin-conflict guard, the ``seen`` bookkeeping, and the readded-to-pack re-enable."""
    label = f"{display_project(binding.project)}/{binding.name}"
    existing = get_stream(binding.name, binding.project)
    if existing is not None and existing["origin"] == "api":
        report["conflicts"].append(label)
        log.warning("sync_pack_streams: %s is origin=api; pack entry skipped", label)
        return
    if not _upsert_binding(binding, actor=actor, origin="pack"):
        # A concurrent write flipped this row's origin between the read above and this write —
        # upsert_stream's own conditional WHERE is what actually stopped it (defense in depth).
        report["conflicts"].append(label)
        log.warning("sync_pack_streams: %s origin changed concurrently; pack entry skipped", label)
        return
    seen.add((binding.project, binding.name))
    report["synced"].append(label)
    if existing is not None and existing["state"] == "disabled":
        restored_state = _sweep_restore_state(existing.get("state_reason"))
        # `restored_state` is `None` for a human's disable/pause reason (or no reason) — those
        # must never be "restored" (fix round 2, N1: only a reason the sweep itself wrote below
        # is ever un-done here).
        if restored_state is not None and set_stream_state(
            binding.project, binding.name, restored_state, only_if_state="disabled"
        ):
            report["enabled"].append(label)
            write_audit_event(
                "dataplane",
                actor,
                "dataplane_stream_enabled",
                label,
                {"reason": "readded_to_pack", "restored_state": restored_state},
            )


def sync_pack_streams(*, actor: str | None = None) -> dict[str, list[Any]]:
    """Scan the active pack's model YAMLs and upsert every declared stream (``origin="pack"``).

    Idempotent: an unchanged pack changes nothing beyond ``updated_at`` — ``upsert_stream`` always
    preserves an existing row's ``state``, so a binding a human paused/disabled stays that way
    across repeated syncs.

    **Ownership (fix round 1, finding 1).** A pack entry never overwrites an existing
    ``origin="api"`` row — it is skipped, logged, and listed under ``report["conflicts"]``. The
    guard is enforced atomically inside ``upsert_stream`` itself, so a concurrent
    :func:`define_stream` call racing this sync cannot be silently clobbered either way.

    **Per-entry robustness (fix round 1, finding 5).** Each YAML file, and each entry within it, is
    parsed/tenancy-checked in its own try/except. A failure — a non-dict entry, an invalid
    ``state:``, a tenancy refusal, a malformed ``inference``/``streams`` shape, or a file that
    fails to parse at all — is recorded under ``report["errors"]`` as
    ``{"file": ..., "entry": <index, "seanerbus_uuid", or None>, "message": ...}`` and every other
    file/entry is still processed.

    **Removal sweep (fix round 1 finding 2; fix round 2, N1).** A previously-synced
    ``origin="pack"`` row whose YAML entry has disappeared is **disabled**, never deleted, and
    audited (``dataplane_stream_disabled``). Its *prior* state is folded into ``state_reason`` so a
    pause is not lost: a row that was ``enabled`` gets ``state_reason="removed_from_pack"``; a row
    that was ``paused`` gets ``"removed_from_pack:paused"``. The sweep itself only runs when
    **all** of: the pack directory exists, it holds at least one ``*.yaml`` file, and
    ``report["errors"]`` is empty — a misconfigured ``EXAMLOPS_USECASE_DIR`` (a relative path
    resolved from the wrong cwd, say) or one broken YAML file/entry must never look like "every
    other stream was intentionally removed". A row this function itself disabled is restored to
    exactly the state it was folded from (``state_reason`` cleared, audited as
    ``dataplane_stream_enabled`` with ``reason="readded_to_pack"`` and the ``restored_state``) the
    next time its YAML entry reappears; a row already ``disabled`` for any other reason (a human's
    disable, with any reason or none) is left **completely untouched** by the sweep — no state
    change, no reason overwrite, no audit. An ``origin="api"`` row is never touched by the sweep
    regardless of ``seen``.

    **Per-entry robustness (fix round 1 finding 5; fix round 2, I5).** Each YAML file, and each
    entry within it, is parsed/tenancy-checked/upserted inside its own ``try``. The construction
    step (:func:`_binding_from_entry`) type-checks every field up front and raises a templated
    ``SpecError`` (naming the field and its type, never its value) for anything malformed —
    including four shapes round 1 missed: a non-``str`` ``project``, a JSON-unencodable ``options``
    value (an unquoted YAML date parses to a ``datetime.date``), a non-``str`` ``address``, and a
    non-``str`` ``connection``. On top of that, the per-entry (and per-file) loop also wraps the
    **whole** remaining body — including the upsert itself — in a last-resort ``except Exception``,
    so a failure this module did not anticipate (a DB-layer error, say) still cannot abort the rest
    of the sync; that branch never logs/records the exception's own text, only its type and the
    file/entry position, since an exception this module did not raise itself may embed the entry's
    values. A failure of any kind is recorded under ``report["errors"]`` as
    ``{"file": ..., "entry": <index, "seanerbus_uuid", or None>, "message": ...}`` and suppresses
    the removal sweep for this run (see above).

    Returns ``{"synced": [...], "disabled": [...], "enabled": [...], "conflicts": [...],
    "errors": [...]}`` — the first four hold ``"project/name"`` labels (``_global`` for the
    unscoped project).
    """
    report: dict[str, list[Any]] = {
        "synced": [],
        "disabled": [],
        "enabled": [],
        "conflicts": [],
        "errors": [],
    }
    seen: set[tuple[str, str]] = set()
    md = models_dir()
    md_is_dir = md.is_dir()
    yaml_paths: list[Path] = (
        [p for p in sorted(md.glob("*.yaml")) if not p.stem.startswith("_")] if md_is_dir else []
    )

    for path in yaml_paths:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the whole sync
            report["errors"].append(
                {
                    "file": path.name,
                    "entry": None,
                    "message": f"{type(exc).__name__}: could not parse YAML",
                }
            )
            # The exception text of a YAML parse error can embed a snippet of the offending line
            # (which may hold an address/URL/option value) — log only the type and the path.
            log.warning("sync_pack_streams: failed to load %s: %s", path, type(exc).__name__)
            continue
        if not isinstance(raw, dict) or not raw.get("name"):
            report["errors"].append(
                {"file": path.name, "entry": None, "message": "missing or invalid 'name'"}
            )
            continue

        try:
            model = str(raw["name"])
            validate_name(model, "model")
            project = normalize_project(raw.get("project"))
            if project:
                validate_name(project, "project")
            inference = raw.get("inference")
            if inference is None:
                inference = {}
            if not isinstance(inference, dict):
                raise SpecError(f"model {model!r}: 'inference' must be a mapping")
            raw_streams = inference.get("streams") or []
            if not isinstance(raw_streams, list):
                raise SpecError(f"model {model!r}: 'inference.streams' must be a list")
        except SpecError as exc:
            report["errors"].append({"file": path.name, "entry": None, "message": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 - last-resort per-file isolation (fix round 2, I5)
            report["errors"].append(
                {"file": path.name, "entry": None, "message": f"unexpected {type(exc).__name__}"}
            )
            log.warning("sync_pack_streams: %s failed: %s", path.name, type(exc).__name__)
            continue

        file_connectors: set[str] = set()
        for idx, entry in enumerate(raw_streams):
            try:
                binding = _binding_from_entry(entry, project=project, model=model)
                _check_tenancy(binding.project, binding.model)
                file_connectors.add(binding.connector)
                _sync_one_binding(binding, actor=actor, report=report, seen=seen)
            except SpecError as exc:
                report["errors"].append({"file": path.name, "entry": idx, "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 - fix round 2, I5: the WHOLE per-entry body,
                # upsert included, is isolated here — never str(exc): an exception this module did
                # not itself raise (a DB-layer error, say) may embed the entry's own values.
                report["errors"].append(
                    {"file": path.name, "entry": idx, "message": f"unexpected {type(exc).__name__}"}
                )
                log.warning(
                    "sync_pack_streams: %s entry %d raised an unexpected %s",
                    path.name,
                    idx,
                    type(exc).__name__,
                )

        try:
            legacy = _legacy_binding(raw, project=project, model=model)
            if legacy is not None and "seanerbus" not in file_connectors:
                _check_tenancy(legacy.project, legacy.model)
                _sync_one_binding(legacy, actor=actor, report=report, seen=seen)
        except SpecError as exc:
            report["errors"].append(
                {"file": path.name, "entry": "seanerbus_uuid", "message": str(exc)}
            )
        except Exception as exc:  # noqa: BLE001 - same last-resort isolation as the entry loop
            report["errors"].append(
                {
                    "file": path.name,
                    "entry": "seanerbus_uuid",
                    "message": f"unexpected {type(exc).__name__}",
                }
            )
            log.warning(
                "sync_pack_streams: %s legacy shim raised an unexpected %s",
                path.name,
                type(exc).__name__,
            )

    sweep_ok = md_is_dir and bool(yaml_paths) and not report["errors"]
    if sweep_ok:
        for row in list_streams():
            if row["origin"] != "pack" or row["state"] == "disabled":
                continue
            key = (row["project"], row["name"])
            if key in seen:
                continue
            prior_state = row["state"]
            reason = _sweep_disable_reason(prior_state)
            label = f"{display_project(row['project'])}/{row['name']}"
            if set_stream_state(
                row["project"],
                row["name"],
                "disabled",
                only_if_state=prior_state,
                reason=reason,
            ):
                report["disabled"].append(label)
                write_audit_event(
                    "dataplane",
                    actor,
                    "dataplane_stream_disabled",
                    label,
                    {"reason": reason, "prior_state": prior_state},
                )
                log.info(
                    "sync_pack_streams: disabled removed pack stream %s (was %s)",
                    label,
                    prior_state,
                )
    return report


# ── API/CLI write path (origin="api") ────────────────────────────────────────


def _check_connector(kind: str) -> None:
    """Refuse a connector kind nothing can run (review M5).

    ``define_stream`` validated the name, the project, the model and tenancy but never the
    ``connector`` string, so a typo (``kafak``) was stored happily and the supervisor then parked
    the stream in ``error`` for ever, with nothing said at definition time. The known kinds are
    the registered :mod:`~examlops.dataplane.streams.connectors` (built-ins plus entry-point
    plugins) plus ``http``, which the service's push route serves rather than a supervised thread.

    Only the API/CLI path checks this: a *pack* entry may legitimately name a connector that a
    site's pack registers later (the SeanerBUS one arrives as pack content), and a pack sync that
    refused it would disable streams a running site depends on.
    """
    from examlops.dataplane.streams import connectors
    from examlops.dataplane.streams.supervisor import PUSH_CONNECTOR

    if kind == PUSH_CONNECTOR:
        return
    try:
        connectors.get(kind)
    except SpecError as exc:
        raise SpecError(f"{exc}, {PUSH_CONNECTOR}") from None


def define_stream(binding: StreamBinding, *, actor: str | None) -> StreamBinding:
    """The API/CLI stream-definition path: name-validated, tenancy-enforced (ruling R11),
    canonical-model-resolved, audited.

    Validates ``binding.name``/``binding.model``/the normalized project against the committed
    ASCII name validator (:func:`examlops.dataplane.safety.validate_name`) **before any lookup**
    (fix round 1, finding 3) — closing a Unicode case-fold gap where SQLite's ASCII-only
    ``lower()`` and Ray Serve's Python ``str.lower()`` disagree on a non-ASCII spelling (e.g. the
    Kelvin sign U+212A), which could let a ``_global`` stream pass R11's "model has no project
    membership" check for a model that is actually scoped under its ASCII-normalized spelling.

    ``binding.model`` is then resolved case-insensitively against the active pack's model YAMLs
    and replaced with the pack's own canonical spelling before it is ever stored (fix round 1,
    finding 4) — telemetry/drift use ``binding.model`` verbatim with no case folding of their own
    (Global Constraints), so storing an uncanonicalised spelling (e.g. the MLflow-cased ``jpcp``)
    would silently split that model's drift history across two names. An unknown model raises
    ``SpecError("unknown model <m>")``.

    ``binding.connector`` must be a kind something can actually run — a registered stream
    connector, or ``http`` for the service's own push route (fix: review M5). A pack entry is not
    held to this, since a pack may register its own connector.

    Refuses (``SpecError``) to create or overwrite a row whose existing ``origin`` is ``"pack"``
    (fix round 1, finding 1) — that row belongs to the use-case pack; change it in the model YAML
    and re-run :func:`sync_pack_streams`. Enforced twice: a pre-check here (for a clear error
    message) and atomically inside :func:`examlops.data.dataplane.upsert_stream` itself, which
    closes the check-then-act race between two processes.

    ``binding.project`` is normalized ("_global" -> "") and ``binding.origin`` is forced to "api"
    regardless of what was passed in. Returns the binding as actually stored.
    """
    validate_name(binding.name, "stream name")
    validate_name(binding.model, "model")
    _check_connector(binding.connector)
    project = normalize_project(binding.project)
    if project:
        validate_name(project, "project")
    canonical_model = _resolve_canonical_model(binding.model)
    label = f"{display_project(project)}/{binding.name}"

    existing = get_stream(binding.name, project)
    if existing is not None and existing["origin"] == "pack":
        raise SpecError(
            f"stream {label} is defined by the use-case pack; change it in the model YAML"
        )
    _check_tenancy(project, canonical_model)
    stored = dataclasses.replace(binding, project=project, model=canonical_model, origin="api")
    if not _upsert_binding(stored, actor=actor):
        # A concurrent pack sync claimed this row between the read above and this write.
        raise SpecError(
            f"stream {label} is defined by the use-case pack; change it in the model YAML"
        )
    write_audit_event(
        "dataplane",
        actor,
        "dataplane_stream_defined",
        label,
        {
            "connector": stored.connector,
            "model": stored.model,
            "alias": stored.alias,
            "state": stored.state,
        },
    )
    return stored
