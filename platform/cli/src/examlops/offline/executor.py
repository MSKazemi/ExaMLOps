"""Offline (batch) inference: run a registered predictive model over a dataset (ADR 0149).

What this runs, and what it deliberately does not
-------------------------------------------------
* **predictive** servables only. ``generative`` and ``agentic`` are valid *kinds* in the spec but
  are refused with ``kind_not_supported_offline`` - their executors (an offline vLLM / Ray Data LLM
  engine with OpenAI-Batch JSONL, an agent worker in a batch allocation) are not built here, and
  the only in-tree route to a generative model is the gateway, which a batch run must not use.
* The run is **inline** in the calling process (the mock/local path, no cluster needed). It is not
  yet submitted through the admission queue or a scheduler, and ``flexibility_s`` > 0 (a
  carbon-aware later start) is refused.

One inference implementation
----------------------------
The model is loaded by ``serving.ray_serving.app.load_model_version`` - the function a serving
replica calls - so artifact verification (``EXAMLOPS_SERVING_VERIFY``), the artifact cache and the
flavour dispatch are the replica's own. Rows are turned into an array by the same OIP v2 code the
online ``/v2`` route uses (``serving.ray_serving.oip.to_array`` / ``model_input``, one tensor per
column, ordered by the model's signature) and ``model.predict`` is called as the replica calls it.

Streaming, resume, idempotency
------------------------------
The input is read in bounded batches (never the whole dataset). Each batch is committed as one
Parquet part plus a small sidecar (row/error tally and the part's sha256), the sidecar written last
and both via an atomic replace. A job is identified by ``(tenant, idempotency_key)``:

* a re-run of a **completed** job returns the stored result (``replayed: true``) and computes
  nothing;
* a re-run of a job that crashed, failed or was cancelled **resumes**: committed batches whose part
  still hashes to its sidecar are skipped, every other batch is recomputed;
* the same key with a different (resolved) spec is refused (``idempotency_conflict``);
* a job whose lease has not expired is refused (``idempotency_in_progress``). The lease is renewed
  after every batch (``EXAMLOPS_OFFLINE_LEASE_TTL``, default 600 s).

A bad row never ends the run: a batch that fails as a whole is retried row by row and only the rows
that fail carry an ``error`` (their ``prediction`` is null); the tally is in the manifest.

Output
------
Content-addressed like a dataplane snapshot - the output revision is
``examlops.data.content_hash.revision_id`` over the parts' digests and schema, so identical
predictions have the identical revision. ``local`` output lands in ``<output>/<revision>/`` with a
``_manifest.json``; ``dataplane`` output is published as a snapshot of the named source, so a
later job (or a training run) can pin it.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from examlops.data import offline as store
from examlops.data.audit import audit_best_effort
from examlops.data.content_hash import file_digest, revision_id, schema_hash, schema_part
from examlops.offline.spec import (
    OfflineJob,
    OfflineSpecError,
    canonical_hash,
    job_id_for,
)

__all__ = [
    "OfflineRefusal",
    "cancel",
    "list_jobs",
    "operation_view",
    "run",
    "status",
]

OUTPUT_TABLE = "predictions"
DEFAULT_LEASE_TTL_S = 600.0
_ERROR_MAX = 300

#: Executor name recorded in every manifest, so an offline output says which path produced it.
ENGINE = "mlflow-pyfunc-oip"


class OfflineRefusal(Exception):
    """An expected refusal, reported as ``{"ok": False, "code": ...}`` - never a stack trace."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message, **extra}


def lease_ttl() -> float:
    try:
        v = float(os.getenv("EXAMLOPS_OFFLINE_LEASE_TTL", ""))
    except ValueError:
        return DEFAULT_LEASE_TTL_S
    return v if v > 0 else DEFAULT_LEASE_TTL_S


def _offline_root() -> Path:
    """Where a dataplane-bound job stages its parts (local output keeps them next to itself)."""
    if explicit := os.getenv("EXAMLOPS_OFFLINE_WORKDIR", "").strip():
        return Path(explicit).expanduser()
    if root := os.getenv("EXAMLOPS_DATA_DIR", "").strip():
        return Path(root).expanduser() / "cache" / "offline"
    xdg = os.getenv("XDG_CACHE_HOME", "").strip()
    return (Path(xdg).expanduser() if xdg else Path.home() / ".cache") / "examlops" / "offline"


def _dataplane_cache() -> Path:
    if explicit := os.getenv("EXAMLOPS_DATAPLANE_CACHE_DIR", "").strip():
        return Path(explicit).expanduser()
    return _offline_root().parent / "dataplane"


# ── input ────────────────────────────────────────────────────────────────────────────────────
class _Input:
    def __init__(
        self, files: list[Path], name: str, revision: str, descriptor: dict[str, Any]
    ) -> None:
        self.files = files
        self.name = name
        self.revision = revision
        self.descriptor = descriptor


def _parquet_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(
        p for p in root.rglob("*.parquet") if not any(x.startswith(".") for x in p.parts[-3:])
    )


def _resolve_input(spec: OfflineJob) -> _Input:
    i = spec.input
    if i.type == "local":
        path = Path(str(i.path)).expanduser()
        if not path.exists():
            raise OfflineRefusal("input_not_found", f"input path {path} does not exist")
        files = _parquet_files(path)
        if not files:
            raise OfflineRefusal("input_not_found", f"no Parquet files under {path}")
        base = path.parent if path.is_file() else path
        digests = [file_digest(f) + str(f.relative_to(base)) for f in files]
        sh = schema_hash(schema_part(f) for f in files)
        rev = revision_id(digests, sh)
        return _Input(
            files,
            path.stem if path.is_file() else path.name,
            rev,
            {"type": "local", "path": str(path)},
        )
    from examlops.dataplane import materialize, read_manifest, resolve, source_key, store_from_env
    from examlops.dataplane.types import DataplaneError

    try:
        st = store_from_env()
        key = source_key(i.project or spec.project, str(i.source))
        ref = resolve(st, key, i.revision)
        manifest = read_manifest(st, ref)
        if i.table not in manifest.tables:
            raise OfflineRefusal(
                "input_not_found",
                f"snapshot {key}@{ref.revision[:12]} has no table {i.table!r} "
                f"(tables: {list(manifest.tables)})",
            )
        local = materialize(st, ref, _dataplane_cache())
    except OfflineRefusal:
        raise
    except DataplaneError as exc:
        raise OfflineRefusal("input_not_found", f"dataplane input: {exc}") from exc
    files = _parquet_files(local / str(i.table))
    if not files:
        raise OfflineRefusal("input_not_found", f"table {i.table!r} of {key} holds no Parquet")
    return _Input(
        files,
        key,
        ref.revision,
        {"type": "dataplane", "source": key, "table": i.table, "uri": st.uri(ref.manifest_key)},
    )


def _count_batches(files: list[Path], batch_size: int) -> tuple[int, int]:
    import pyarrow.parquet as pq

    rows = [int(pq.read_metadata(f).num_rows) for f in files]
    return sum(math.ceil(n / batch_size) for n in rows), sum(rows)


def _iter_batches(files: list[Path], batch_size: int) -> Iterator[tuple[int, int, Any]]:
    """``(batch_index, first_row_id, pyarrow Table)`` - exactly ``batch_size`` rows each except the
    last of a file; the split is a pure function of the files and the batch size (resume relies
    on it), and never holds more than about two batches."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    idx = offset = 0
    for f in files:
        buf: list[Any] = []
        held = 0
        for rb in pq.ParquetFile(f).iter_batches(batch_size=batch_size):
            buf.append(rb)
            held += rb.num_rows
            while held >= batch_size:
                tbl = pa.Table.from_batches(buf)
                yield idx, offset, tbl.slice(0, batch_size)
                idx, offset = idx + 1, offset + batch_size
                rest = tbl.slice(batch_size)
                buf, held = rest.to_batches(), rest.num_rows
        if held:
            tbl = pa.Table.from_batches(buf)
            yield idx, offset, tbl
            idx, offset = idx + 1, offset + held


# ── model ────────────────────────────────────────────────────────────────────────────────────
def _resolve_version(model: str, version: str | None, alias: str | None) -> str:
    import mlflow

    client = mlflow.MlflowClient()
    try:
        if version:
            return str(client.get_model_version(model, str(version)).version)
        return str(client.get_model_version_by_alias(model, str(alias)).version)
    except Exception as exc:  # noqa: BLE001 - any registry failure is a refusal, not a crash
        want = f"version {version}" if version else f"alias {alias!r}"
        raise OfflineRefusal("model_not_found", f"{model} {want}: {exc}") from exc


def _default_loader(model: str, version: str) -> Any:
    """Load exactly as a serving replica does (by immutable version, never by alias)."""
    import mlflow

    from serving.ray_serving.app import load_model_version

    mv = mlflow.MlflowClient().get_model_version(model, version)
    return load_model_version(model, None, mv)


class _Scorer:
    """Rows in, predictions out - through the online path's own OIP v2 conversion."""

    def __init__(self, model: Any) -> None:
        from serving.ray_serving import oip

        self.oip = oip
        self.model = model
        self.signature = oip.signature_of(model)

    def check_schema(self, schema: Any) -> None:
        if self.signature is None:
            return
        missing = [n for n in self.signature.names if n not in schema.names]
        if missing:
            raise OfflineRefusal(
                "input_schema_mismatch",
                f"the input lacks the model's signature columns {missing}; "
                f"it has {list(schema.names)}",
            )

    def _body(self, table: Any) -> dict[str, Any]:
        import pyarrow as pa

        names = list(self.signature.names) if self.signature else list(table.column_names)
        tensors = []
        for name in names:
            col = table.column(name)
            t = col.type
            rows = col.to_pylist()
            if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t):
                k = len(rows[0]) if rows and rows[0] is not None else 0
                data = [x for r in rows for x in (r if r is not None else [None] * k)]
                shape = [len(rows), k]
            else:
                data, shape = rows, [len(rows)]
            tensors.append({"name": name, "shape": shape, "datatype": "FP64", "data": data})
        return {"inputs": tensors}

    def predict(self, table: Any) -> list[Any]:
        try:
            array = self.oip.to_array(self._body(table), self.signature)
        except self.oip.ProtocolError as exc:
            raise ValueError(str(exc)) from exc
        raw = self.model.predict(self.oip.model_input(array, self.signature))
        result = raw.tolist() if hasattr(raw, "tolist") else list(raw)
        if len(result) != table.num_rows:
            raise ValueError(
                f"the model returned {len(result)} predictions for {table.num_rows} rows"
            )
        return result


def _score_batch(scorer: _Scorer, table: Any) -> tuple[list[Any], list[str | None]]:
    """Whole-batch first; on any failure, row by row so one bad row costs one row."""
    n = table.num_rows
    try:
        return scorer.predict(table), [None] * n
    except Exception:  # noqa: BLE001 - isolate the failing rows below
        preds: list[Any] = []
        errs: list[str | None] = []
        for r in range(n):
            try:
                preds.extend(scorer.predict(table.slice(r, 1)))
                errs.append(None)
            except Exception as exc:  # noqa: BLE001
                preds.append(None)
                errs.append(str(exc)[:_ERROR_MAX] or type(exc).__name__)
        return preds, errs


def _output_table(offset: int, preds: list[Any], errs: list[str | None]) -> Any:
    import pyarrow as pa

    pred = pa.array(preds)
    if pa.types.is_null(pred.type):
        pred = pred.cast(pa.float64())
    return pa.table(
        {
            "row_id": pa.array(range(offset, offset + len(preds)), pa.int64()),
            "prediction": pred,
            "error": pa.array(errs, pa.string()),
        }
    )


# ── part commit / resume ────────────────────────────────────────────────────────────────────
def _text(path: Path, text: str) -> None:
    path.write_text(text)


def _write_atomic(path: Path, write: Callable[[Path], None]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    write(tmp)
    os.replace(tmp, path)


def _part(work: Path, idx: int) -> tuple[Path, Path]:
    return (
        work / OUTPUT_TABLE / f"part-{idx:05d}.parquet",
        work / OUTPUT_TABLE / f"part-{idx:05d}.json",
    )


def _committed(work: Path, idx: int) -> dict[str, Any] | None:
    """The sidecar of a committed batch, or None when it must be (re)computed. A part that no
    longer hashes to its sidecar - a torn or altered file - is not committed."""
    part, side = _part(work, idx)
    try:
        info = json.loads(side.read_text())
        if info.get("idx") == idx and file_digest(part) == info["sha256"]:
            return info
    except (OSError, ValueError, KeyError):
        pass
    return None


def _commit(work: Path, idx: int, out: Any, ok: int, errors: int) -> dict[str, Any]:
    import pyarrow.parquet as pq

    part, side = _part(work, idx)
    part.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(part, lambda p: pq.write_table(out, p))
    info = {
        "idx": idx,
        "rows": out.num_rows,
        "ok": ok,
        "errors": errors,
        "sha256": file_digest(part),
    }
    _write_atomic(side, lambda p: _text(p, json.dumps(info, sort_keys=True)))
    return info


# ── run ──────────────────────────────────────────────────────────────────────────────────────
def _version(dist: str) -> str:
    try:
        from importlib.metadata import version

        return version(dist)
    except Exception:  # noqa: BLE001
        return ""


def run(
    spec: OfflineJob,
    *,
    actor: str | None = None,
    loader: Callable[[str, str], Any] | None = None,
    resolve_version: Callable[[str, str | None, str | None], str] | None = None,
    on_batch: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run (or resume, or replay) one offline job inline. Always returns a dict; ``ok`` is False
    with a ``code`` for every expected refusal or failure.

    ``loader``/``resolve_version`` default to the serving replica's loader and the MLflow registry;
    they are seams for callers that hold a model already, not a second inference implementation.
    ``on_batch`` is called with each committed batch's tally (progress; and where a test crashes).
    """
    try:
        spec.validate()
    except OfflineSpecError as exc:
        return _err("invalid_spec", str(exc), problems=exc.problems)
    if spec.kind != "predictive":
        return _err(
            "kind_not_supported_offline",
            f"offline {spec.kind} inference is not built: only predictive servables run offline "
            "today (ADR 0149 decision 1; generative needs an offline engine executor, agentic an "
            "agent batch worker)",
            kind=spec.kind,
        )
    actor = actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    job_id = job_id_for(spec.tenant, spec.idempotency_key)
    try:
        version = (resolve_version or _resolve_version)(spec.model, spec.version, spec.alias)
        inp = _resolve_input(spec)
        resolved = {
            **spec.to_dict(),
            "version": version,
            "alias": spec.alias,
            "input_revision": inp.revision,
        }
        spec_hash = canonical_hash(resolved)
        outcome, row = store.claim(
            job_id,
            tenant=spec.tenant,
            key=spec.idempotency_key,
            spec_hash=spec_hash,
            spec=resolved,
            kind=spec.kind,
            model=spec.model,
            model_version=version,
            actor=actor,
            lease_ttl_s=lease_ttl(),
        )
    except OfflineRefusal as exc:
        return _err(exc.code, exc.message, **exc.extra)
    except Exception as exc:  # noqa: BLE001 - datastore trouble is a refusal, never a half-run
        return _err("offline_unavailable", f"offline job store unavailable: {exc}")
    if outcome == "conflict":
        return _err(
            "idempotency_conflict",
            "idempotency_key was already used for a different request (model version, input "
            "revision or parameters differ); not applied",
            job_id=job_id,
        )
    if outcome == "in_progress":
        return _err(
            "idempotency_in_progress",
            f"job {job_id} is being run by another process; retry shortly",
            job_id=job_id,
        )
    if outcome == "completed":
        return {**(row.get("result") or {"ok": True, "job_id": job_id}), "replayed": True}

    t0 = time.monotonic()
    work = _workdir(spec, job_id)
    try:
        result = _execute(spec, job_id, version, inp, resolved, work, outcome, loader, on_batch)
    except OfflineRefusal as exc:
        store.finish(job_id, "failed", error=f"{exc.code}: {exc.message}")
        _audit(actor, spec, job_id, "offline_failed", {"code": exc.code})
        return _err(exc.code, exc.message, job_id=job_id, state="failed", **exc.extra)
    except Exception as exc:  # noqa: BLE001 - recorded; committed batches stay for the resume
        store.finish(job_id, "failed", error=str(exc)[:_ERROR_MAX])
        _audit(actor, spec, job_id, "offline_failed", {"error": str(exc)[:_ERROR_MAX]})
        return _err("offline_failed", str(exc), job_id=job_id, state="failed")
    if result.get("state") == "cancelled":
        store.finish(job_id, "cancelled", result=None)
        _audit(actor, spec, job_id, "offline_cancelled", {"batches_done": result["counts"]})
        return result
    result["cost"] = _record_cost(spec, version, job_id, time.monotonic() - t0, result["counts"])
    _emit_lineage(spec, job_id, version, inp, result)
    store.finish(
        job_id,
        "completed",
        result=result,
        output_revision=result["output"]["revision"],
        output_uri=result["output"]["uri"],
    )
    shutil.rmtree(work, ignore_errors=True)
    _audit(
        actor,
        spec,
        job_id,
        "offline_completed",
        {
            "model": spec.model,
            "version": version,
            "input_revision": inp.revision,
            "output_revision": result["output"]["revision"],
            **{k: v for k, v in result["counts"].items()},
        },
    )
    return result


def _audit(actor: str | None, spec: OfflineJob, job_id: str, action: str, details: dict) -> None:
    audit_best_effort(
        "offline", actor, action, job_id, {"model": spec.model, **details}, tenant=spec.tenant
    )


def _workdir(spec: OfflineJob, job_id: str) -> Path:
    if spec.output.type == "local":
        return Path(str(spec.output.path)).expanduser() / ".work" / job_id
    return _offline_root() / job_id


def _execute(
    spec: OfflineJob,
    job_id: str,
    version: str,
    inp: _Input,
    resolved: dict[str, Any],
    work: Path,
    outcome: str,
    loader: Callable[[str, str], Any] | None,
    on_batch: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    import pyarrow.parquet as pq

    try:
        model = (loader or _default_loader)(spec.model, version)
    except Exception as exc:  # noqa: BLE001
        raise OfflineRefusal(
            "model_load_failed", f"could not load {spec.model} v{version}: {exc}"
        ) from exc
    scorer = _Scorer(model)
    scorer.check_schema(pq.read_schema(inp.files[0]))
    total, total_rows = _count_batches(inp.files, spec.batch_size)
    store.set_total(job_id, total)
    (work / OUTPUT_TABLE).mkdir(parents=True, exist_ok=True)

    infos: list[dict[str, Any]] = []
    skipped = 0
    ok_rows = err_rows = 0
    for idx, offset, table in _iter_batches(inp.files, spec.batch_size):
        info = _committed(work, idx)
        if info is not None:
            skipped += 1
        else:
            preds, errs = _score_batch(scorer, table)
            n_err = sum(1 for e in errs if e is not None)
            info = _commit(work, idx, _output_table(offset, preds, errs), len(preds) - n_err, n_err)
        infos.append(info)
        ok_rows += info["ok"]
        err_rows += info["errors"]
        cancel_requested = store.progress(
            job_id,
            batches_done=len(infos),
            rows_ok=ok_rows,
            rows_err=err_rows,
            lease_ttl_s=lease_ttl(),
        )
        if on_batch is not None:
            on_batch({**info, "batches_done": len(infos), "batches_total": total})
            cancel_requested = cancel_requested or store.cancel_requested(job_id)
        if cancel_requested:
            return {
                "ok": False,
                "code": "cancelled",
                "job_id": job_id,
                "state": "cancelled",
                "counts": {"batches": len(infos), "rows_ok": ok_rows, "rows_err": err_rows},
            }
    counts = {
        "batches": len(infos),
        "skipped_batches": skipped,
        "rows": total_rows,
        "rows_ok": ok_rows,
        "rows_err": err_rows,
    }
    return _finalize(spec, job_id, version, inp, resolved, work, infos, counts, outcome)


def _finalize(
    spec: OfflineJob,
    job_id: str,
    version: str,
    inp: _Input,
    resolved: dict[str, Any],
    work: Path,
    infos: list[dict[str, Any]],
    counts: dict[str, Any],
    outcome: str,
) -> dict[str, Any]:
    parts = [_part(work, i["idx"])[0] for i in infos]
    sh = schema_hash(schema_part(p) for p in parts)
    revision = revision_id([i["sha256"] for i in infos], sh)
    manifest = {
        "kind": "offline-inference",
        "schema_version": 1,
        "job_id": job_id,
        "revision": revision,
        "input": {**inp.descriptor, "revision": inp.revision},
        "model": {"name": spec.model, "version": version, "alias": spec.alias},
        "engine": {
            "executor": ENGINE,
            "mlflow": _version("mlflow"),
            "examlops": _version("examlops"),
        },
        "parameters": {"batch_size": spec.batch_size, "workload_kind": spec.kind},
        "counts": counts,
        "batches": [{k: i[k] for k in ("idx", "rows", "ok", "errors", "sha256")} for i in infos],
        "reproducibility": "deterministic-given-model-and-input",
    }
    if spec.output.type == "local":
        dest = Path(str(spec.output.path)).expanduser() / revision
        if not (dest / OUTPUT_TABLE).exists():
            dest.mkdir(parents=True, exist_ok=True)
            os.replace(work / OUTPUT_TABLE, dest / OUTPUT_TABLE)
        for side in (dest / OUTPUT_TABLE).glob("*.json"):
            side.unlink()  # the manifest carries the tallies; the revision covers the parquet only
        _write_atomic(dest / "_manifest.json", lambda p: _text(p, json.dumps(manifest, indent=2)))
        out = {"type": "local", "uri": str(dest), "revision": revision}
    else:
        out = _publish(spec, job_id, work, infos, manifest, revision)
    return {
        "ok": True,
        "job_id": job_id,
        "state": "completed",
        "replayed": False,
        "resumed": outcome == "resumed",
        "kind": spec.kind,
        "model": manifest["model"],
        "input": manifest["input"],
        "output": out,
        "counts": counts,
        "engine": manifest["engine"],
    }


def _publish(
    spec: OfflineJob,
    job_id: str,
    work: Path,
    infos: list[dict[str, Any]],
    manifest: dict[str, Any],
    revision: str,
) -> dict[str, Any]:
    from examlops.dataplane import source_key, store_from_env
    from examlops.dataplane.store import publish

    st = store_from_env()
    key = source_key(spec.output.project or spec.project, str(spec.output.source))
    staged = [(f"{OUTPUT_TABLE}/{p.name}", p) for p in (_part(work, i["idx"])[0] for i in infos)]
    snap, _changed = publish(
        st,
        key,
        staged=staged,
        parent=None,
        connector="offline-inference",
        connection=None,
        spec_hash=canonical_hash({"job": job_id, "revision": revision}),
        watermark={
            "offline_job": job_id,
            "model": manifest["model"],
            "input": manifest["input"],
        },
        pull_id=job_id,
        incremental=False,
    )
    st.write_text(f"{key}/{job_id}/_offline_manifest.json", json.dumps(manifest, indent=2))
    return {
        "type": "dataplane",
        "uri": st.uri(f"{key}/{job_id}/_manifest.json"),
        "revision": snap.revision,
        "source": key,
    }


# ── cost + lineage hooks (never fail the run) ───────────────────────────────────────────────
def _record_cost(
    spec: OfflineJob, version: str, job_id: str, wall_s: float, counts: dict[str, Any]
) -> dict[str, Any]:
    """Book this attempt's hours in the ledger ``exa models cost`` reads. The hours are the
    *declared* resources times wall clock - nothing meters an inline run - and a run that
    declared none is "not metered", not zero."""
    r = spec.resources
    if not r.gpus and not r.cpus:
        return {"metered": False, "basis": "no resources declared; nothing recorded"}
    gpu_h = r.gpus * wall_s / 3600.0
    cpu_h = r.cpus * wall_s / 3600.0
    cost: dict[str, Any] = {
        "metered": False,
        "basis": "declared resources x wall clock of this attempt",
        "gpu_hours": round(gpu_h, 6),
        "cpu_hours": round(cpu_h, 6),
        "items": counts["rows"],
    }
    try:
        from examlops.data.finops import record_model_cost
        from examlops.finops.cost import estimate_cost_via_provider

        usd = estimate_cost_via_provider(gpu_h, cpu_h).get("cost_usd")
        record_model_cost(spec.model, int(version), None, job_id, gpu_h, usd, cpu_hours=cpu_h)
        cost["cost_usd"] = usd
        if usd is not None and counts["rows"]:
            cost["cost_usd_per_item"] = usd / counts["rows"]
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not undo a finished run
        cost["recorded"] = False
        cost["error"] = str(exc)[:_ERROR_MAX]
    return cost


def _emit_lineage(
    spec: OfflineJob, job_id: str, version: str, inp: _Input, result: dict[str, Any]
) -> None:
    """input revision + model version -> offline output, through ``examlops.lineage`` (fail-open)."""
    try:
        from examlops.lineage import dataset_node, emit_lineage, model_node

        out = result["output"]
        out_name = out.get("source") or Path(out["uri"]).parent.name
        emit_lineage(
            "COMPLETE",
            "offline-inference",
            job_id,
            inputs=[dataset_node(inp.name, inp.revision), model_node(spec.model, version)],
            outputs=[dataset_node(out_name, out["revision"])],
            facets={
                "offline_job": job_id,
                "workload_kind": spec.kind,
                "batch_size": spec.batch_size,
                "engine": ENGINE,
                "engine_version": _version("mlflow"),
            },
            dataset_revision=out["revision"],
            model=spec.model,
            model_version=version,
        )
        result["lineage"] = {"emitted": True}
    except Exception as exc:  # noqa: BLE001
        result["lineage"] = {"emitted": False, "error": str(exc)[:_ERROR_MAX]}


# ── read / control ──────────────────────────────────────────────────────────────────────────
def _view(row: dict[str, Any]) -> dict[str, Any]:
    live = row["state"] == "running" and (row.get("lease_expires") or 0) > time.time()
    state = row["state"]
    if state == "running" and not live:
        state = "stalled"  # a crashed runner: re-running the same command resumes it
    return {
        "job_id": row["job_id"],
        "state": state,
        "kind": row["kind"],
        "model": row["model"],
        "model_version": row["model_version"],
        "batches_done": row["batches_done"],
        "batches_total": row["batches_total"],
        "rows_ok": row["rows_ok"],
        "rows_err": row["rows_err"],
        "attempts": row["attempts"],
        "cancel_requested": bool(row["cancel_requested"]),
        "output_revision": row["output_revision"],
        "output_uri": row["output_uri"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def status(job_id: str) -> dict[str, Any]:
    try:
        row = store.get(job_id)
    except Exception as exc:  # noqa: BLE001
        return _err("offline_unavailable", str(exc))
    if row is None:
        return _err("not_found", f"no offline job {job_id!r}")
    return {"ok": True, "job": _view(row)}


def list_jobs(*, state: str | None = None, limit: int = 50) -> dict[str, Any]:
    try:
        rows = store.list_jobs(state=state, limit=max(1, min(int(limit), 200)))
    except Exception as exc:  # noqa: BLE001
        return _err("offline_unavailable", str(exc))
    return {"ok": True, "jobs": [_view(r) for r in rows]}


def cancel(job_id: str, *, actor: str | None = None) -> dict[str, Any]:
    """Cooperative cancel. A live runner stops after the batch in flight (``cancelled`` stays false
    until it has); a job nobody is running is cancelled at once. Completed work is kept: re-running
    the same key resumes it."""
    try:
        outcome, row = store.request_cancel(job_id)
    except Exception as exc:  # noqa: BLE001
        return _err("offline_unavailable", str(exc))
    if outcome == "not_found" or row is None:
        return _err("not_found", f"no offline job {job_id!r}")
    if outcome == "not_cancellable":
        return _err(
            "not_cancellable",
            f"job {job_id} is already {row['state']}",
            cancelled=False,
            job=_view(row),
        )
    audit_best_effort(
        "offline",
        actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli",
        "offline_cancel_requested",
        job_id,
        {"outcome": outcome},
        tenant=row["tenant"],
    )
    return {"ok": True, "cancelled": outcome == "cancelled", "outcome": outcome, "job": _view(row)}


_OP_STATE = {
    "queued": "working",
    "running": "working",
    "stalled": "working",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def operation_view(job_id: str) -> dict[str, Any]:
    """The job as an ``exa ops`` operation document (:mod:`examlops.operations` vocabulary)."""
    out = status(job_id)
    if not out.get("ok"):
        return out
    j = out["job"]
    state = _OP_STATE.get(j["state"], "unknown")
    return {
        "ok": True,
        "operation": {
            "operation_id": j["job_id"],
            "kind": "offline_inference",
            "state": state,
            "terminal": state in ("completed", "failed", "cancelled"),
            "cancellable": j["state"] in ("queued", "running", "stalled", "failed"),
            "raw_state": j["state"],
            "run_state": None,
            "attempts": j["attempts"],
            "flow_run_id": None,
            "last_error": j["error"],
            "created_at": j["created_at"],
            "updated_at": j["updated_at"],
            "detail": f"{j['batches_done']}/{j['batches_total'] or '?'} batches, "
            f"{j['rows_err']} row error(s)",
        },
    }
