"""Whole-platform backup & recovery (Phase 0 item 0.9, extended).

The platform is many data stores: the ``platform.db`` monolith (data layer + event bus + security
store), the control-plane / agent SQLite DBs, the operator's on-disk config, the MLflow + Prefect
Postgres metadata, and the MinIO object buckets (artifacts, datasets, project storage). Losing any
of them — or restoring them inconsistently — is a blast-radius event.

This package provides a **tiered backup bundle**: one directory capturing every tier, each with a
verifiable manifest, created on a schedule (the Compose ``backup`` sidecar) and before risky ops,
replicated off-site to S3/MinIO with retention, and restorable as a verified round trip. Heavy tiers
degrade to ``skipped`` when their tooling/endpoint is absent, so the control-plane profile
(``sqlite`` + ``config``) always succeeds with zero external dependencies.

The four legacy single-DB functions (:func:`create_backup`, :func:`verify_backup`,
:func:`restore_backup`, :func:`list_backups`) are preserved unchanged from the original
``examlops.backup`` module (now in :mod:`examlops.backup.sqlite_tier`) and re-exported here, so
existing imports (``from examlops import backup``) and ``exa backup create|verify|restore`` keep
working byte-for-byte.
"""

from __future__ import annotations

from .auto import auto_backup_before
from .bundle import (
    BundleResult,
    create_bundle,
    list_bundles,
    restore_bundle,
    verify_bundle,
)
from .sqlite_tier import (
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)

__all__ = [
    # legacy single-DB API (unchanged)
    "create_backup",
    "verify_backup",
    "restore_backup",
    "list_backups",
    # tiered-bundle API
    "create_bundle",
    "verify_bundle",
    "restore_bundle",
    "list_bundles",
    "BundleResult",
    "auto_backup_before",
]
