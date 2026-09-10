"""The instance-data layer — where everything a site creates after install lives (ADR 0128).

ExaMLOps is three layers that change at different speeds and belong to different people:

* **core** — the product code (``examlops``, ``pipelines``, ``serving``, the services). Replaced
  wholesale on every upgrade; never holds site state.
* **deployment** — how the core is run (Docker Compose, the Helm chart, a bare host). Owned by the
  operator; carries endpoints, secrets and resource shapes, never user content.
* **instance data** — what users create once the platform is running: models and their metadata,
  pipelines, projects, datasets, the audit chain, site configuration, the use-case pack. It must
  survive every upgrade, be backed up as one thing, and be portable to a new install.

This module gives the third layer a single, optional root: ``EXAMLOPS_DATA_DIR``. When it is set,
the defaults of the instance-data locations derive from it (an explicitly set per-store variable
such as ``PLATFORM_DB`` still wins). When it is unset every location keeps its historical default,
so an existing install behaves byte-for-byte as before.

The code deliberately does **not** read ``EXAMLOPS_STATE_DIR``: that is a Compose host path, and
``env_file: .env`` injects the *host* value into containers, where it names nothing. Compose sets
``EXAMLOPS_DATA_DIR=/state`` on every service that mounts the state directory instead.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DATA_DIR_ENV = "EXAMLOPS_DATA_DIR"
DEPLOYMENT_ENV = "EXAMLOPS_DEPLOYMENT"

#: The canonical layout of a data root. Each entry: relative path → what it holds. ``exa instance
#: init`` creates the directories; files are created by the component that owns them.
LAYOUT: dict[str, str] = {
    "platform.db": "platform datastore (SQLite engine) — audit chain, projects, drift, lineage, …",
    "site.toml": "site profile — which modules this centre runs (ADR 0128)",
    "usecase/": "the site's active use-case pack — its models, pipelines and datasets (ADR 0094)",
    "config/": "site configuration — clusters.yaml, policy.yaml, finops.yaml, providers.yaml",
    ".providers/": "calculation providers authored from notebooks",
    ".feature_store/": "feature-store files",
    "agent/": "the Skipper agent's checkpoints, long-term memory and memory-review queue",
    "backups/": "local backup bundles (replicate them off-site)",
}

# Directories ``exa instance init`` creates. Files are left to their owners.
_INIT_DIRS = ("usecase", "config", ".providers", "backups", "agent")

# Beyond this many files a directory's size is reported as a lower bound, so ``exa instance info``
# stays fast on a large pack or backup directory.
_SIZE_SCAN_LIMIT = 20_000


def data_root() -> Path | None:
    """The configured data root, or ``None`` when the install uses the legacy per-store defaults."""
    raw = os.getenv(DATA_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def data_path(*parts: str) -> Path | None:
    """A path under the data root, or ``None`` when no data root is configured."""
    root = data_root()
    return root.joinpath(*parts) if root is not None else None


def deployment_kind() -> str:
    """How this process is being run: ``kubernetes``, ``container``, ``host`` (or the override).

    ``EXAMLOPS_DEPLOYMENT`` wins (the Helm chart sets ``kubernetes``, Compose sets ``compose``).
    Otherwise the platform's own markers decide: the service-account env every pod gets, then the
    files Docker and Podman create inside a container.
    """
    override = os.getenv(DEPLOYMENT_ENV, "").strip().lower()
    if override:
        return override
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return "container"
    return "host"


def redact_dsn(dsn: str) -> str:
    """A connection string with its password removed — safe to print or put in a report."""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "<unparseable dsn>"
    if parts.password is None:
        return dsn
    netloc = parts.netloc.rsplit("@", 1)[-1]
    user = parts.username or ""
    return urlunsplit((parts.scheme, f"{user}:***@{netloc}", parts.path, parts.query, ""))


@dataclass
class Location:
    """One place instance data lives, as reported by ``exa instance info``."""

    name: str
    kind: str  # sqlite | file | dir | external
    where: str
    source: str  # the env var that set it, "data-root", or "default"
    exists: bool
    size_bytes: int | None
    backed_up_by: str  # the backup tier that captures it, or "not captured"
    in_data_root: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _size(path: Path) -> int | None:
    try:
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return None
        total = 0
        for n, p in enumerate(path.rglob("*")):
            if n >= _SIZE_SCAN_LIMIT:
                break
            if p.is_file():
                total += p.stat().st_size
        return total
    except OSError:
        return None


def _under_root(path: Path) -> bool:
    root = data_root()
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _local(name: str, kind: str, path: Path, source: str, tier: str) -> Location:
    return Location(
        name=name,
        kind=kind,
        where=str(path),
        source=source,
        exists=path.exists(),
        size_bytes=_size(path),
        backed_up_by=tier,
        in_data_root=_under_root(path),
    )


def _env_path(var: str, default: Path | str | None) -> tuple[Path | None, str]:
    value = os.getenv(var)
    if value:
        return Path(value).expanduser(), var
    if default is None:
        return None, "default"
    return Path(default).expanduser(), "default"


def agent_db_default(filename: str) -> str:
    """Where the agent keeps ``filename`` when its ``AGENT_*`` variable is unset.

    Mirrors ``skipper.config._agent_state`` (the agent package cannot import this one in every
    image): ``<data root>/agent/<file>`` with a data root, else the historical CWD-relative path.
    """
    root = data_root()
    return str(root / "agent" / filename) if root is not None else f"./{filename}"


def config_dir() -> Path:
    """The site configuration directory (clusters/policy/finops/providers YAML).

    ``EXAMLOPS_CONFIG_DIR`` wins, then ``<data root>/config`` when a data root is set, then
    ``~/.config/examlops``. The single rule the policy, provider and HPC-registry loaders share —
    deliberately *not* the parent of ``EXAMLOPS_CONFIG``, which is the operator's own CLI config
    and, in the dashboard container, a private volume.
    """
    if raw := os.getenv("EXAMLOPS_CONFIG_DIR"):
        return Path(raw).expanduser()
    if (root := data_root()) is not None:
        return root / "config"
    return Path.home() / ".config" / "examlops"


def inventory() -> list[Location]:
    """Every place this install keeps instance data, with where it is and what backs it up.

    Pure inspection: nothing is created or opened for writing. External stores (Postgres, the
    object store) are reported by endpoint with credentials redacted.
    """
    from examlops.platform_db import _db_path

    out: list[Location] = []
    root = data_root()

    if os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres":
        dsn = os.getenv("EXAMLOPS_POSTGRES_DSN", "")
        out.append(
            Location(
                "platform datastore",
                "external",
                redact_dsn(dsn) if dsn else "<EXAMLOPS_POSTGRES_DSN unset>",
                "EXAMLOPS_POSTGRES_DSN",
                bool(dsn),
                None,
                "postgres",
                False,
            )
        )
    else:
        source = "PLATFORM_DB" if os.getenv("PLATFORM_DB") else ("data-root" if root else "default")
        out.append(_local("platform datastore", "sqlite", Path(_db_path()), source, "sqlite"))

    for name, var, default in (
        ("control-plane store", "CONTROL_PLANE_DB", None),
        ("agent checkpoints", "AGENT_DB", agent_db_default("agent_memory.db")),
        ("agent long-term memory", "AGENT_MEMORY_DB", agent_db_default("skipper_memory.db")),
        (
            "agent memory review queue",
            "AGENT_MEMORY_REVIEW_DB",
            agent_db_default("skipper_review.db"),
        ),
    ):
        path, source = _env_path(var, default)
        if source == "default" and root is not None and var.startswith("AGENT_"):
            source = "data-root"
        if path is not None:
            out.append(_local(name, "sqlite", path, source, "sqlite"))

    tracking = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:15000")
    out.append(
        Location(
            "model registry + run metadata (MLflow)",
            "external",
            redact_dsn(tracking),
            "MLFLOW_TRACKING_URI" if os.getenv("MLFLOW_TRACKING_URI") else "default",
            True,
            None,
            "postgres",
            False,
        )
    )
    s3 = os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:19000")
    out.append(
        Location(
            "model artifacts + datasets + project storage (object store)",
            "external",
            redact_dsn(s3),
            "MLFLOW_S3_ENDPOINT_URL" if os.getenv("MLFLOW_S3_ENDPOINT_URL") else "default",
            True,
            None,
            "objects",
            False,
        )
    )

    from examlops.cli._config import config_path

    cli_cfg = config_path()
    out.append(
        _local(
            "operator CLI config",
            "file",
            cli_cfg,
            "EXAMLOPS_CONFIG" if os.getenv("EXAMLOPS_CONFIG") else "default",
            "config",
        )
    )
    cdir = config_dir()
    if os.getenv("EXAMLOPS_CONFIG_DIR"):
        csource = "EXAMLOPS_CONFIG_DIR"
    else:
        csource = "data-root" if root is not None else "default"
    out.append(_local("site configuration", "dir", cdir, csource, "config"))

    from examlops.lifecycle.modules import site_profile_path

    profile, psource = site_profile_path()
    out.append(_local("site profile (modules)", "file", profile, psource, "config"))

    pack, pack_source = usecase_pack_dir()
    if pack is not None:
        out.append(_local("use-case pack (models, pipelines)", "dir", pack, pack_source, "config"))

    providers, psrc = _env_path("EXAMLOPS_PROVIDERS_DIR", root / ".providers" if root else None)
    if providers is not None:
        out.append(
            _local(
                "authored providers",
                "dir",
                providers,
                psrc if psrc != "default" else "data-root",
                "config",
            )
        )
    fstore, fsrc = _env_path("FEATURE_STORE_DIR", Path(_db_path()).parent / ".feature_store")
    if fstore is not None:
        out.append(_local("feature store", "dir", fstore, fsrc, "config"))

    from examlops.backup._config import load as backup_config

    bdir = Path(backup_config().out_dir).expanduser()
    out.append(
        _local(
            "backup bundles",
            "dir",
            bdir,
            "EXAMLOPS_BACKUP_DIR" if os.getenv("EXAMLOPS_BACKUP_DIR") else "default",
            "off-site replica (backup --push)",
        )
    )
    return out


def usecase_pack_dir() -> tuple[Path | None, str]:
    """The active use-case pack directory and what selected it (mirrors the pack loaders)."""
    if raw := os.getenv("EXAMLOPS_USECASE_DIR"):
        return Path(raw).expanduser(), "EXAMLOPS_USECASE_DIR"
    if (pack := data_path("usecase")) is not None and (pack / "pack.toml").is_file():
        return pack, "data-root"
    try:
        from examlops.usecase import models_dir

        return models_dir().parent, "default"
    except Exception:  # noqa: BLE001 — inspection must never fail on an odd pack layout
        return None, "default"


def init_layout(
    root: Path, *, pack: Path | None = None, overwrite_pack: bool = False
) -> dict[str, Any]:
    """Create the data-root directories and, optionally, seed the site's use-case pack.

    Idempotent: existing directories are kept, an existing pack is never overwritten unless
    ``overwrite_pack`` is set. Returns what was created, for the CLI to report.
    """
    root = root.expanduser()
    created: list[str] = []
    root.mkdir(parents=True, exist_ok=True)
    for rel in _INIT_DIRS:
        d = root / rel
        if not d.exists():
            d.mkdir(parents=True)
            created.append(f"{rel}/")
    seeded: str | None = None
    if pack is not None:
        pack = pack.expanduser()
        if not (pack / "pack.toml").is_file():
            raise ValueError(f"{pack} is not a use-case pack (no pack.toml)")
        target = root / "usecase"
        has_pack = (target / "pack.toml").is_file()
        if has_pack and not overwrite_pack:
            seeded = None
        else:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(
                pack, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
            )
            seeded = str(pack)
    return {"root": str(root), "created": created, "pack_seeded_from": seeded}
