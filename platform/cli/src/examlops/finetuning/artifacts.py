"""The adapter as a file and as an MLflow artifact (ADR 0044 clause 2).

Two jobs, both about *where an adapter lives* once a run has trained it:

* **The bundle.** :func:`write_bundle` writes ``<run>/adapter/adapter_model.pt`` (the trained
  tensors only — the LoRA factors, or every weight for a full fine-tune) next to
  ``adapter_config.json`` (backend, method, rank, alpha, seed, the frozen base's digest and the
  tensors' digest). :func:`load_bundle` is **verify-before-load**: it refuses a bundle whose
  tensors do not hash to the digest the registry recorded, so a swapped or corrupted file is never
  served. ``torch.load`` runs with ``weights_only=True`` — no pickle code executes.
* **MLflow.** :func:`log_to_mlflow` records the adapter as a first-class MLflow artifact: one run
  in experiment ``finetune/<base>`` carrying the parameters, the *measured* metrics, provenance
  tags (dataset revision, adapter digest, training run, ``eval_source=measured``) and the bundle
  itself under ``adapter/``. A full fine-tune is also registered as a normal **model version**
  (clause 4). Best effort by design: with no ``MLFLOW_TRACKING_URI``, or an unreachable server,
  it returns a result saying so and the adapter stays registered in ``platform.db`` — the
  platform's system of record — rather than failing a training run that succeeded. Every HTTP
  call is bounded: 30 s and two retries unless MLflow's own `MLFLOW_HTTP_REQUEST_TIMEOUT` /
  `MLFLOW_HTTP_REQUEST_MAX_RETRIES` are set.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

BUNDLE_DIRNAME = "adapter"
WEIGHTS_FILE = "adapter_model.pt"
CONFIG_FILE = "adapter_config.json"
BUNDLE_FORMAT = "examlops-adapter/1"


class BundleError(RuntimeError):
    """A bundle is missing, malformed, or does not match the digest the registry recorded."""


# ── bundle ────────────────────────────────────────────────────────────────────


def bundle_dir(run_dir: Path | str) -> Path:
    return Path(run_dir) / BUNDLE_DIRNAME


def write_bundle(run_dir: Path | str, state: dict[str, Any], meta: dict[str, Any]) -> Path:
    """Write the trained tensors + their config atomically; returns the bundle directory."""
    import io

    import torch

    from examlops.distributed import checkpoint_files as cf

    directory = bundle_dir(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    torch.save({k: v.contiguous() for k, v in state.items()}, buf)
    cf.atomic_write_bytes(directory / WEIGHTS_FILE, buf.getvalue())
    config = {"format": BUNDLE_FORMAT, **meta, "tensors": sorted(state)}
    cf.atomic_write_bytes(
        directory / CONFIG_FILE, json.dumps(config, indent=2, sort_keys=True).encode()
    )
    return directory


def read_config(directory: Path | str) -> dict[str, Any]:
    path = Path(directory) / CONFIG_FILE
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BundleError(f"adapter bundle config {path} is unreadable: {exc}") from exc
    if not isinstance(config, dict) or config.get("format") != BUNDLE_FORMAT:
        raise BundleError(f"{path} is not an {BUNDLE_FORMAT} bundle")
    return config


def load_bundle(
    directory: Path | str, *, expected_sha256: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load ``(tensors, config)`` — only after the tensors hash to ``expected_sha256``.

    ``expected_sha256`` is the digest the registry holds for this adapter. ``None`` is refused:
    an adapter with no recorded digest has nothing to verify against, and serving it would load
    whatever file happens to be on disk.
    """
    import torch

    from examlops.finetuning import lora

    if not expected_sha256:
        raise BundleError("the registry holds no adapter digest to verify this bundle against")
    config = read_config(directory)
    path = Path(directory) / WEIGHTS_FILE
    try:
        state = torch.load(path, map_location="cpu", weights_only=True)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is the same finding
        raise BundleError(f"adapter weights {path} are unreadable: {exc}") from exc
    if not isinstance(state, dict) or not state:
        raise BundleError(f"{path} holds no tensors")
    actual = lora.adapter_sha256(state)
    if actual != expected_sha256:
        raise BundleError(
            f"adapter weights {path} hash to {actual[:12]}…, the registry recorded "
            f"{expected_sha256[:12]}… — refusing to load a bundle that is not the one registered"
        )
    return state, config


# ── MLflow ────────────────────────────────────────────────────────────────────


@dataclass
class MlflowRecord:
    status: str  # logged | skipped | failed
    reason: str | None = None
    run_id: str | None = None
    experiment: str | None = None
    artifact_uri: str | None = None
    model_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tracking_uri(explicit: str | None) -> str | None:
    return explicit or os.getenv("MLFLOW_TRACKING_URI") or None


@contextmanager
def _bounded_http() -> Iterator[None]:
    """Cap MLflow's HTTP timeout and retries for the duration of one logging call."""
    wanted = {"MLFLOW_HTTP_REQUEST_TIMEOUT": "30", "MLFLOW_HTTP_REQUEST_MAX_RETRIES": "2"}
    saved = {k: os.environ.get(k) for k in wanted}
    for key, value in wanted.items():
        if saved[key] is None:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)


def _numeric(metrics: dict[str, Any]) -> dict[str, float]:
    keys = (
        "eval_score",
        "eval_loss",
        "eval_n",
        "baseline_eval_score",
        "first_loss",
        "final_loss",
        "trainable_parameters",
    )
    out: dict[str, float] = {}
    for key in keys:
        value = metrics.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(value)
    return out


def log_to_mlflow(
    adapter_id: str,
    *,
    base: str,
    method: str,
    dataset_revision: str | None,
    metrics: dict[str, Any],
    bundle: Path | str,
    train_run_id: str | None,
    tracking_uri: str | None = None,
    mlflow_module: Any = None,
) -> MlflowRecord:
    """Record the adapter as an MLflow run + artifact; register a full fine-tune as a version.

    Never raises for an environment fact (no server configured, server down, mlflow not
    installed): the result says what happened and the caller audits it.
    """
    uri = _tracking_uri(tracking_uri)
    if not uri:
        return MlflowRecord("skipped", reason="MLFLOW_TRACKING_URI is not set")
    bundle_path = Path(bundle)
    if not bundle_path.is_dir():
        return MlflowRecord("failed", reason=f"adapter bundle {bundle_path} does not exist")
    if mlflow_module is None:
        try:
            import mlflow as mlflow_module  # noqa: PLC0415 - optional, heavy
        except ImportError:
            return MlflowRecord("skipped", reason="mlflow is not installed")
    mlflow = mlflow_module
    experiment = f"finetune/{base}"
    try:
        with _bounded_http():
            mlflow.set_tracking_uri(uri)
            mlflow.set_experiment(experiment)
            tags = {
                "examlops.kind": "lora_adapter" if method != "full" else "full_finetune",
                "examlops.adapter_id": adapter_id,
                "examlops.base_ref": base,
                "examlops.dataset_revision": dataset_revision or "",
                "examlops.adapter_sha256": str(metrics.get("adapter_sha256") or ""),
                "examlops.train_run_id": train_run_id or "",
                "examlops.eval_source": "measured",
                "examlops.data": str(metrics.get("data") or ""),
            }
            with mlflow.start_run(run_name=adapter_id, tags=tags) as run:
                mlflow.log_params(
                    {
                        "base_ref": base,
                        "method": method,
                        "backend": metrics.get("backend"),
                        "rank": metrics.get("rank"),
                        "alpha": metrics.get("alpha"),
                        "lr": metrics.get("lr"),
                        "steps": metrics.get("steps"),
                        "batch": metrics.get("batch"),
                        "seed": metrics.get("seed"),
                        "dataset_revision": dataset_revision,
                    }
                )
                mlflow.log_metrics(_numeric(metrics))
                mlflow.log_artifacts(str(bundle_path), artifact_path=BUNDLE_DIRNAME)
                run_id = run.info.run_id
                artifact_uri = f"{run.info.artifact_uri}/{BUNDLE_DIRNAME}"
            version: str | None = None
            if method == "full":
                # Clause 4: a full fine-tune is a normal model version, not an adapter delta.
                # The source is the logged bundle itself (MLflow 3's `register_model(runs:/…)`
                # resolves only LoggedModels, which a plain artifact directory is not).
                client = mlflow.MlflowClient()
                try:
                    client.create_registered_model(adapter_id)
                except Exception as exc:  # noqa: BLE001 - already registered is fine
                    if "RESOURCE_ALREADY_EXISTS" not in str(exc) and "already exists" not in str(
                        exc
                    ):
                        raise
                mv = client.create_model_version(
                    adapter_id,
                    source=artifact_uri,
                    run_id=run_id,
                    tags={"examlops.kind": "full_finetune"},
                )
                version = str(mv.version)
    except Exception as exc:  # noqa: BLE001 - an unreachable tracker is an environment fact
        return MlflowRecord("failed", reason=f"{type(exc).__name__}: {exc}"[:500])
    return MlflowRecord(
        "logged",
        run_id=run_id,
        experiment=experiment,
        artifact_uri=artifact_uri,
        model_version=version,
    )


__all__ = [
    "BUNDLE_DIRNAME",
    "BUNDLE_FORMAT",
    "BundleError",
    "MlflowRecord",
    "bundle_dir",
    "load_bundle",
    "log_to_mlflow",
    "read_config",
    "write_bundle",
]
