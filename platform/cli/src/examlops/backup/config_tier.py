"""Config + on-disk content tier.

Captures the operator's on-disk configuration (``~/.config/examlops/`` — config.toml, clusters.yaml,
policy.yaml, finops.yaml, providers/) as a tarball, and — opt-in — the use-case content
(``usecases/`` packs, ``pipelines/envs/*.yaml``, the ``.dualgit/`` classification) which normally lives in git.

**Secrets caveat (deliberate).** The secrets *ciphertext* lives in ``platform.db`` (sqlite tier),
but it is useless without the KEK, which is held in the ``EXAMLOPS_SECRETS_KEYS`` env — never on
disk. We therefore record the *key ids* present and emit a loud warning that the KEK must be backed
up out-of-band; we never write the plaintext KEK into the bundle. A passphrase-encrypted keyring
export is a deferred enhancement.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path
from typing import Any

from ._manifest import OK, SKIPPED, TierResult, sha256_file


def _config_dir() -> Path:
    """The directory holding the CLI config tree (parent of the effective config.toml)."""
    override = os.getenv("EXAMLOPS_CONFIG")
    if override:
        return Path(override).parent
    return Path.home() / ".config" / "examlops"


def _secrets_key_ids() -> list[str]:
    """Key ids present in the KEK keyring — recorded so a restore knows which KEKs it needs."""
    raw = os.getenv("EXAMLOPS_SECRETS_KEYS", "")
    ids = [p.split(":", 1)[0].strip() for p in raw.split(",") if ":" in p and p.strip()]
    if os.getenv("EXAMLOPS_SECRETS_KEY"):
        ids.append("primary")
    if os.getenv("DASHBOARD_SECRET_KEY"):
        ids.append("legacy-dashboard")
    return sorted(set(ids))


def _tar_dir(src: Path, dest: Path, *, arcname: str) -> int:
    """tar+gzip ``src`` into ``dest``; return byte size. Excludes obvious junk."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        base = Path(ti.name).name
        if base in {"__pycache__", ".DS_Store"} or base.endswith(".pyc"):
            return None
        return ti

    with tarfile.open(dest, "w:gz") as tar:
        tar.add(src, arcname=arcname, filter=_filter)
    return dest.stat().st_size


def _repo_root() -> Path | None:
    """Best-effort repo root (for the optional use-case content). ``EXAMLOPS_REPO_ROOT`` wins."""
    if root := os.getenv("EXAMLOPS_REPO_ROOT"):
        return Path(root)
    # Walk up from CWD looking for the workspace marker.
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / "usecases").is_dir() and (parent / "platform").is_dir():
            return parent
    return None


# Instance-data content kept under the data root (ADR 0128) — user-owned, so always captured.
# The datastores themselves are the sqlite tier's; backups/ is where bundles land.
_DATA_ROOT_CONTENT = ("site.toml", "usecase", "config", ".providers")


def _data_root_items(config_out: Path) -> list[dict[str, Any]]:
    """Tar the site's own content under ``EXAMLOPS_DATA_DIR`` (profile, pack, site config).

    Also captures the site configuration directory and the site profile when they live outside
    the data root (``EXAMLOPS_CONFIG_DIR`` / ``EXAMLOPS_SITE_PROFILE``) and outside the CLI config
    tree this tier already captures — the files the policy, provider and HPC loaders read.
    """
    from examlops.lifecycle.datadir import config_dir, data_root
    from examlops.lifecycle.modules import site_profile_path

    items: list[dict[str, Any]] = []
    captured: set[Path] = {_config_dir().resolve()}
    root = data_root()
    if root is not None and root.is_dir():
        for rel in _DATA_ROOT_CONTENT:
            src = root / rel
            if not src.exists():
                continue
            arc = "data-root_" + rel.lstrip(".").replace("/", "_")
            dest = config_out / f"{arc}.tar.gz"
            size = _tar_dir(src, dest, arcname=rel)
            captured.add(src.resolve())
            items.append(
                {
                    "name": f"data-root:{rel}",
                    "file": f"config/{arc}.tar.gz",
                    "source": str(src),
                    "restore_to": "data-root",
                    "sha256": sha256_file(dest),
                    "size_bytes": size,
                    "status": OK,
                }
            )
    for label, src in (("site-config", config_dir()), ("site-profile", site_profile_path()[0])):
        if not src.exists() or any(
            src.resolve() == c or c in src.resolve().parents for c in captured
        ):
            continue
        dest = config_out / f"{label}.tar.gz"
        size = _tar_dir(src, dest, arcname=src.name)
        captured.add(src.resolve())
        items.append(
            {
                "name": label,
                "file": f"config/{label}.tar.gz",
                "source": str(src),
                "sha256": sha256_file(dest),
                "size_bytes": size,
                "status": OK,
            }
        )
    return items


def backup_config_tier(dest_dir: Path, *, with_content: bool = False) -> TierResult:
    """Tar the config tree (+ optional use-case content) into ``dest_dir/config/``."""
    config_out = dest_dir / "config"
    config_out.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []

    cfg_dir = _config_dir()
    key_ids = _secrets_key_ids()
    warnings: list[str] = []
    if key_ids:
        warnings.append(
            "KEK not bundled — restore of encrypted secrets requires the same "
            "EXAMLOPS_SECRETS_KEYS env (backed up out-of-band)."
        )

    if cfg_dir.is_dir():
        dest = config_out / "config-tree.tar.gz"
        size = _tar_dir(cfg_dir, dest, arcname="examlops-config")
        items.append(
            {
                "file": "config/config-tree.tar.gz",
                "source": str(cfg_dir),
                "sha256": sha256_file(dest),
                "size_bytes": size,
                "secrets_key_ids": key_ids,
                "kek_present_in_bundle": False,
                "warnings": warnings,
                "status": OK,
            }
        )
    else:
        items.append(
            {
                "name": "config-tree",
                "status": SKIPPED,
                "reason": f"config dir not present at {cfg_dir}",
                "secrets_key_ids": key_ids,
                "warnings": warnings,
            }
        )

    items.extend(_data_root_items(config_out))

    if with_content:
        root = _repo_root()
        if root is None:
            items.append({"name": "usecases", "status": SKIPPED, "reason": "repo root not found"})
        else:
            for rel in ("usecases", "pipelines/envs"):
                src = root / rel
                if not src.exists():
                    items.append({"name": rel, "status": SKIPPED, "reason": f"{rel} not present"})
                    continue
                arc = rel.replace("/", "_")
                dest = config_out / f"{arc}.tar.gz"
                size = _tar_dir(src, dest, arcname=arc)
                items.append(
                    {
                        "name": rel,
                        "file": f"config/{arc}.tar.gz",
                        "source": str(src),
                        "sha256": sha256_file(dest),
                        "size_bytes": size,
                        "status": OK,
                    }
                )
            # The dual-git classification. Worth capturing because
            # `.dualgit/exclude.public.txt` is the ONLY backup of the public leak
            # firewall: `.git/info/exclude` is repo-local and on no remote, so if it
            # is lost the next public `git add -A` would stage the entire private
            # tree. Restore with `dualgit firewall restore`.
            dualgit_dir = root / ".dualgit"
            if dualgit_dir.is_dir():
                dest = config_out / "dualgit.tar.gz"
                size = _tar_dir(dualgit_dir, dest, arcname="dualgit")
                items.append(
                    {
                        "name": ".dualgit",
                        "file": "config/dualgit.tar.gz",
                        "source": str(dualgit_dir),
                        "sha256": sha256_file(dest),
                        "size_bytes": size,
                        "status": OK,
                    }
                )

    from ._manifest import rollup_status

    status = rollup_status([i["status"] for i in items])
    return TierResult("config", status=status, items=items)


def restore_config_tier(bundle_dir: Path, *, dest_dir: str | None = None) -> list[dict[str, Any]]:
    """Extract the config tarball back to ``dest_dir`` (default: the live config dir).

    Use-case content tarballs are extracted next to the repo root when present. This never
    overwrites secrets: the ciphertext is in the sqlite tier and needs the out-of-band KEK.
    """
    import json

    manifest_path = bundle_dir / "bundle.manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    out: list[dict[str, Any]] = []
    target = Path(dest_dir) if dest_dir else _config_dir()
    for item in manifest.get("tiers", {}).get("config", {}).get("items", []):
        if item.get("status") != OK or "file" not in item:
            continue
        archive = bundle_dir / item["file"]
        if item["file"].endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as tar:
                if item.get("name", "").startswith("config") or "config-tree" in item["file"]:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tar.extractall(target.parent, filter="data")  # noqa: S202 — our own bundle
                    out.append({"file": item["file"], "restored_to": str(target.parent)})
                elif item.get("restore_to") == "data-root":
                    from examlops.lifecycle.datadir import data_root

                    root = data_root()
                    if root is None:
                        out.append(
                            {
                                "file": item["file"],
                                "skipped": "EXAMLOPS_DATA_DIR is not set on this install",
                            }
                        )
                        continue
                    root.mkdir(parents=True, exist_ok=True)
                    tar.extractall(root, filter="data")  # noqa: S202 — our own bundle
                    out.append({"file": item["file"], "restored_to": str(root)})
        # Non-config content tarballs are left for the operator to place deliberately.
    return out
