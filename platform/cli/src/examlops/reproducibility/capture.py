"""ADR 0038 clause 1 — collectors that *fill* the manifest instead of waiting to be told.

Before this module the manifest accepted feature views, scheduler resources, hardware, a
lineage run id and an image digest, but no platform path supplied them: every bundle that was
not hand-built on the command line recorded them empty. Each collector here reads the source of
truth that already exists and records exactly what it observed — never a guess:

* :func:`capture_code_commits` — the platform checkout **and the upstream model library**
  (``EXAMLOPS_MODELZOO_DIR``, default ``./modelzoo``), each with its own commit and dirty flag.
  A library that is not a git checkout is recorded by its installed distribution version and
  marked ``commit: null``; nothing claims a commit that was not read.
* :func:`capture_hardware` — machine, OS, CPU count, memory and the GPUs ``nvidia-smi`` reports.
  A host without ``nvidia-smi`` is ``gpu_probe: "unavailable"``, not ``gpus: []`` presented as
  "no GPUs".
* :func:`capture_resources` — the scheduler-neutral request the pipeline actually submitted
  (``EXAMLOPS_HPC_*``), the scheduler name and the job id.
* :func:`capture_image_digest` — ``EXAMLOPS_IMAGE_DIGEST``, which a container build sets to its
  own digest. Unset outside a container: recorded as ``null``.
* :func:`capture_feature_views` — A3 feature views by name, versioned by the SHA-256 of their
  definition (the store keeps no version counter; a definition hash is what can be re-checked).
* :func:`attach_bom` — the D3 AI-BOM of the version: the existing one when there is one, else a
  CycloneDX BOM generated from the bundle's own package set. Its canonical hash is recorded so
  ``verify`` detects a BOM that changed after the bundle was built.

Every collector is bounded (subprocess timeouts, capped lists) and never raises into the caller
except :func:`capture_feature_views`, which refuses an unknown view name: a bundle that says it
pinned a view that does not exist would be a false record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

ENV_MODELZOO_DIR = "EXAMLOPS_MODELZOO_DIR"
ENV_IMAGE_DIGEST = "EXAMLOPS_IMAGE_DIGEST"
ENV_FEATURE_VIEWS = "EXAMLOPS_FEATURE_VIEWS"
MODELZOO_DISTRIBUTION = "seanergys-modelzoo"

_GIT_TIMEOUT = 10
_NVIDIA_TIMEOUT = 5
MAX_GPUS = 64
MAX_FEATURE_VIEWS = 64
_DIGEST = re.compile(r"^(?:[\w./:-]+@)?sha256:[0-9a-f]{64}$")

# Scheduler-neutral request keys, as ``pipelines.pipeline_generator._hpc_resources`` builds
# them, mapped to the environment variable that sets each one. Exported again by
# ``--execute`` so a rebuild requests what the original run requested.
RESOURCE_ENV: dict[str, str] = {
    "partition": "EXAMLOPS_HPC_PARTITION",
    "qos": "EXAMLOPS_HPC_QOS",
    "account": "EXAMLOPS_HPC_ACCOUNT",
    "constraint": "EXAMLOPS_HPC_CONSTRAINT",
    "time": "EXAMLOPS_HPC_TIME",
    "nodes": "EXAMLOPS_HPC_NODES",
    "ntasks": "EXAMLOPS_HPC_NTASKS",
    "cpus_per_task": "EXAMLOPS_HPC_CPUS",
    "mem": "EXAMLOPS_HPC_MEM",
    "gpus": "EXAMLOPS_HPC_GPUS",
}


class UnknownFeatureViewError(KeyError):
    """A bundle was asked to pin a feature view that is not registered.

    A ``KeyError`` subclass for existing callers; a CLI catches *this* type, so an unrelated
    ``KeyError`` bug inside bundle building is never reported as an unknown view.
    """


def canonical_sha256(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


# ── code ─────────────────────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=_GIT_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_state(repo: Path) -> dict[str, Any] | None:
    """``{"commit", "dirty", "path"}`` of a checkout, or ``None`` when ``repo`` is not one.

    ``dirty`` is ``None`` when the status could not be read — never ``False`` by default.
    """
    if not repo.is_dir():
        return None
    head = _git(repo, "rev-parse", "HEAD")
    if head is None or head.returncode != 0 or not head.stdout.strip():
        return None
    status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    dirty: bool | None
    if status is None or status.returncode != 0:
        dirty = None
    else:
        dirty = bool(status.stdout.strip())
    top = _git(repo, "rev-parse", "--show-toplevel")
    path = top.stdout.strip() if top is not None and top.returncode == 0 else str(repo)
    return {"commit": head.stdout.strip(), "dirty": dirty, "path": path}


def modelzoo_dir(base: Path | None = None) -> Path:
    raw = (os.getenv(ENV_MODELZOO_DIR) or "").strip()
    return Path(raw) if raw else (base or Path.cwd()) / "modelzoo"


def capture_code_commits(repo: Path | None = None) -> dict[str, dict[str, Any]]:
    """Commits of every repository whose code a training run executes.

    ``platform`` is the checkout the bundle is built in; ``modelzoo`` is the upstream model
    library. When the library is installed but not a git checkout, its distribution version is
    recorded with ``commit: null`` — a version pin, stated as exactly that.
    """
    base = repo or Path.cwd()
    out: dict[str, dict[str, Any]] = {}
    plat = git_state(base)
    if plat is not None:
        out["platform"] = plat
    mz_path = modelzoo_dir(base)
    mz = git_state(mz_path)
    # A modelzoo dir that is merely a sub-directory of the platform checkout is not its own
    # repository: its "commit" would be the platform's, which says nothing about the library.
    if mz is not None and plat is not None and mz["path"] == plat["path"]:
        mz = None
    if mz is not None:
        out["modelzoo"] = mz
    else:
        try:
            from importlib import metadata

            ver = metadata.version(MODELZOO_DISTRIBUTION)
        except Exception:  # noqa: BLE001 - not installed: nothing to record
            ver = None
        if ver:
            out["modelzoo"] = {"commit": None, "dirty": None, "distribution_version": ver}
    return out


# ── hardware ─────────────────────────────────────────────────────────────────────────────────


def _memory_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        return None


def probe_gpus() -> tuple[str, list[dict[str, str]]]:
    """``(probe status, gpus)`` from ``nvidia-smi``; status ``ok``/``unavailable``/``error``."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "unavailable", []
    try:
        done = subprocess.run(
            [
                exe,
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_NVIDIA_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "error", []
    if done.returncode != 0:
        return "error", []
    gpus: list[dict[str, str]] = []
    for line in done.stdout.splitlines()[:MAX_GPUS]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0]:
            gpus.append({"name": parts[0], "memory_mib": parts[1], "driver": parts[2]})
    return "ok", gpus


def capture_hardware() -> dict[str, Any]:
    uname = os.uname() if hasattr(os, "uname") else None
    status, gpus = probe_gpus()
    return {
        "machine": uname.machine if uname else None,
        "system": uname.sysname if uname else None,
        "kernel": uname.release if uname else None,
        "cpu_count": os.cpu_count(),
        "memory_bytes": _memory_bytes(),
        "gpu_probe": status,
        "gpus": gpus,
    }


def describe_hardware_difference(recorded: dict[str, Any], current: dict[str, Any]) -> str | None:
    """One line naming what differs between two hardware records, or ``None``.

    Only facts that affect numerics are compared: architecture and GPU models. A different CPU
    count or memory size is not a reproduction difference worth reporting.
    """
    diffs: list[str] = []
    if recorded.get("machine") and recorded.get("machine") != current.get("machine"):
        diffs.append(f"arch {recorded.get('machine')} -> {current.get('machine')}")
    if recorded.get("gpu_probe") == "ok" and current.get("gpu_probe") == "ok":
        a = sorted(g.get("name", "") for g in recorded.get("gpus") or [])
        b = sorted(g.get("name", "") for g in current.get("gpus") or [])
        if a != b:
            diffs.append(f"GPUs {a or 'none'} -> {b or 'none'}")
    elif recorded.get("gpu_probe") == "ok" and recorded.get("gpus"):
        diffs.append(f"GPUs recorded {len(recorded['gpus'])}, cannot probe here")
    return "; ".join(diffs) or None


# ── scheduler resources ──────────────────────────────────────────────────────────────────────


def capture_resources(
    requested: dict[str, Any] | None,
    *,
    scheduler: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """The scheduler-neutral request as submitted, plus which scheduler ran it and the job id."""
    req = {k: str(v) for k, v in (requested or {}).items() if k in RESOURCE_ENV and v is not None}
    out: dict[str, Any] = {"requested": req}
    if scheduler:
        out["scheduler"] = scheduler
    if job_id:
        out["job_id"] = job_id
    return out


# Legacy fallbacks ``_hpc_resources`` reads when the EXAMLOPS_HPC_* variable is unset.
LEGACY_RESOURCE_ENV: tuple[str, ...] = (
    "EXAMLOPS_SLURM_PARTITION",
    "EXAMLOPS_SLURM_TIME",
    "EXAMLOPS_SLURM_NODES",
    "EXAMLOPS_SLURM_CPUS",
    "EXAMLOPS_SLURM_MEM",
)


def resource_env_vars() -> tuple[str, ...]:
    """Every environment variable that feeds the scheduler-neutral request."""
    return (*RESOURCE_ENV.values(), *LEGACY_RESOURCE_ENV)


def resource_env(resources: dict[str, Any]) -> dict[str, str]:
    """``EXAMLOPS_HPC_*`` variables that re-request the recorded resources."""
    req = resources.get("requested") if isinstance(resources.get("requested"), dict) else resources
    return {RESOURCE_ENV[k]: str(v) for k, v in (req or {}).items() if k in RESOURCE_ENV}


# ── image ────────────────────────────────────────────────────────────────────────────────────


def capture_image_digest() -> str | None:
    """``EXAMLOPS_IMAGE_DIGEST`` when it is a digest (``[repo@]sha256:<64 hex>``), else ``None``.

    A malformed value is not recorded: the ``env`` step would later try to verify it and a
    value that cannot be a digest can only produce a misleading ``absent``.
    """
    raw = (os.getenv(ENV_IMAGE_DIGEST) or "").strip()
    return raw if raw and _DIGEST.match(raw) else None


# ── feature views ────────────────────────────────────────────────────────────────────────────


def feature_view_version(view: dict[str, Any]) -> str:
    """SHA-256 of the parts of a view that decide what a model trains on."""
    return canonical_sha256(
        {
            "entity": view.get("entity"),
            "features": view.get("features"),
            "source": view.get("source"),
            "ttl_seconds": view.get("ttl_seconds"),
            "dataset_revision": view.get("dataset_revision"),
        }
    )


def feature_view_names_from_env() -> list[str]:
    raw = os.getenv(ENV_FEATURE_VIEWS) or ""
    return [n.strip() for n in raw.split(",") if n.strip()][:MAX_FEATURE_VIEWS]


def capture_feature_views(names: list[str]) -> dict[str, dict[str, Any]]:
    """``{name: {"version", "dataset_revision"}}``; raises ``KeyError`` naming unknown views."""
    from examlops.data.data_assets import get_feature_view

    out: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name in list(dict.fromkeys(names))[:MAX_FEATURE_VIEWS]:
        view = get_feature_view(name)
        if view is None:
            missing.append(name)
            continue
        out[name] = {
            "version": feature_view_version(view),
            "dataset_revision": view.get("dataset_revision"),
        }
    if missing:
        raise UnknownFeatureViewError(f"feature view(s) not registered: {', '.join(missing)}")
    return out


# ── AI-BOM ───────────────────────────────────────────────────────────────────────────────────


def attach_bom(
    model: str,
    version: str,
    *,
    packages: dict[str, str],
    dataset_name: str | None,
    dataset_revision: str | None,
) -> dict[str, Any]:
    """Link (or create) the D3 AI-BOM of this version; return ``{"sha256", "source", ...}``.

    An existing BOM is never overwritten — it may carry an eval summary or a framework the
    bundle does not know. Only when the version has none is one generated, from the bundle's
    own recorded package set.
    """
    from examlops.data.registry import get_model_bom

    existing = get_model_bom(model, version)
    source = "existing"
    if existing is None:
        from examlops.supplychain import generate_ai_bom

        existing = generate_ai_bom(
            model,
            version,
            dataset=dataset_name,
            dataset_revision=dataset_revision,
            dependencies=[f"{k}=={v}" for k, v in sorted(packages.items())],
        )
        source = "bundle"
    return {
        "sha256": canonical_sha256(existing),
        "source": source,
        "components": len(existing.get("components") or []),
    }


def current_bom_sha256(model: str, version: str) -> str | None:
    from examlops.data.registry import get_model_bom

    bom = get_model_bom(model, version)
    return canonical_sha256(bom) if bom is not None else None


__all__ = [
    "ENV_FEATURE_VIEWS",
    "ENV_IMAGE_DIGEST",
    "ENV_MODELZOO_DIR",
    "LEGACY_RESOURCE_ENV",
    "RESOURCE_ENV",
    "UnknownFeatureViewError",
    "attach_bom",
    "canonical_sha256",
    "capture_code_commits",
    "capture_feature_views",
    "capture_hardware",
    "capture_image_digest",
    "capture_resources",
    "current_bom_sha256",
    "describe_hardware_difference",
    "feature_view_names_from_env",
    "feature_view_version",
    "git_state",
    "modelzoo_dir",
    "probe_gpus",
    "resource_env",
    "resource_env_vars",
]
