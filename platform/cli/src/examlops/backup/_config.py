"""Backup settings — env vars (with an optional ``[backup]`` TOML section; env wins).

These are deliberately NOT folded into ``cli/_config._FIELDS``: that spec splits every field into a
url/auth pair (``_URL_KEYS``), which would misclassify backup settings. Backup config is flat and
lives here, read straight from the environment (and, if present, a ``[backup]`` table in the CLI
config.toml).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _toml_backup_section() -> dict[str, object]:
    try:
        import tomllib

        from examlops.cli._config import config_path

        p = config_path()
        if p.exists():
            data = tomllib.loads(p.read_text())
            section = data.get("backup", {})
            if isinstance(section, dict):
                return section
    except Exception:  # noqa: BLE001 — config is best-effort; env is the source of truth
        pass
    return {}


def _get(env: str, toml_key: str, default: str) -> str:
    if (val := os.getenv(env)) is not None:
        return val
    section = _toml_backup_section()
    if toml_key in section:
        return str(section[toml_key])
    return default


@dataclass
class BackupConfig:
    out_dir: str
    tiers: list[str]
    interval_s: int
    retain: str
    s3_uri: str
    on_promote: bool


def load() -> BackupConfig:
    """Resolve the effective backup configuration (env > [backup] TOML > default)."""
    tiers = [t.strip() for t in _get("EXAMLOPS_BACKUP_TIERS", "tiers", "sqlite,config").split(",")]
    return BackupConfig(
        out_dir=_get("EXAMLOPS_BACKUP_DIR", "dir", "./backups"),
        tiers=[t for t in tiers if t],
        interval_s=int(_get("EXAMLOPS_BACKUP_INTERVAL", "interval", "3600")),
        retain=_get("EXAMLOPS_BACKUP_RETAIN", "retain", "keep=14"),
        s3_uri=_get("EXAMLOPS_BACKUP_S3_URI", "s3_uri", ""),
        on_promote=_get("EXAMLOPS_BACKUP_ON_PROMOTE", "on_promote", "false").lower()
        in ("1", "true", "yes", "on"),
    )


def parse_retain(spec: str) -> dict[str, int]:
    """Parse a retain spec like ``keep=14`` / ``days=30`` / ``keep=14,days=30`` → kwargs."""
    out: dict[str, int] = {}
    for part in spec.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in ("keep", "days") and v.isdigit():
            out[{"keep": "keep_n", "days": "keep_days"}[k]] = int(v)
    return out
