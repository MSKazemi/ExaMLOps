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
        # Non-config content tarballs are left for the operator to place deliberately.
    return out
