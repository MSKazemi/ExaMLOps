"""Start the container the RPO depends on, and read what it actually wrote.

`docs/guides/backup-restore.md` publishes an objective — **RPO ≤ 1 h** — and the thing that meets it
is a container: the Compose `backup` sidecar running `exa backup schedule`. The release workflow
builds and signs that image (`ghcr.io/mskazemi/examlops-backup`), the Compose file wires it, and
several unit guards check the *YAML*: the profile, the mounts, the read-only MinIO credential.
Nothing ever ran it.

An SLO whose mechanism has never been started is a wish. The image can break in ways no YAML guard
can see — a missing extra, an entrypoint that does not accept the command, a `pip install` that
stopped resolving, a directory it cannot write — and every one of those is invisible until the day
somebody needs the backup.

So this drill builds the image from this tree, runs **one real cycle**, and then treats the
container's output as evidence rather than as a green tick:

* the cycle exits 0 and names the bundle it made;
* the manifest says the **platform datastore** was captured — not merely that the cycle ran, since
  a bundle of nothing but skipped tiers also "succeeds";
* the bundle **verifies** using the host's own `verify_bundle`, so the checksums the container
  wrote are the ones the restore path will check;
* and the database inside it still holds the seeded audit chain, which is the only proof that what
  was copied is a usable datastore rather than a file of the right size.

Opt in with `EXAMLOPS_CHAOS_LIVE=1`; `make chaos-drills` runs it with the rest of the set.

    EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_backup_sidecar_live.py -v -s
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_CHAOS_LIVE") != "1", reason="set EXAMLOPS_CHAOS_LIVE=1"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker"),
]

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "exa-chaos/examlops-backup:tree"
DOCKERFILE = "platform/infra/docker-compose/Dockerfile.backup"


@pytest.fixture(scope="module")
def image() -> str:
    """The sidecar image, built from this tree exactly as Compose builds it."""
    started = time.monotonic()
    build = subprocess.run(
        ["docker", "build", "-q", "-f", DOCKERFILE, "-t", IMAGE, "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, (
        "the backup sidecar image does not build from this tree — the release workflow publishes "
        f"this Dockerfile as an image operators run:\n{build.stderr[-2000:]}"
    )
    print(f"\nbuilt {IMAGE} in {time.monotonic() - started:.0f}s")
    return IMAGE


def _seed(state: Path) -> int:
    """A datastore with a chained audit log, written by this repo's own code."""
    code = (
        "import sys; sys.path.insert(0, 'platform/cli/src')\n"
        "from examlops.platform_db import init_db\n"
        "from examlops.data.audit import write_audit_event, verify_audit_chain\n"
        "init_db()\n"
        "for i in range(5): write_audit_event('exa', 'operator', 'promote', f'JPCP-{i}')\n"
        "print(verify_audit_chain()['count'])\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PLATFORM_DB": str(state / "platform.db")},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return int(out.stdout.strip().splitlines()[-1])


def test_the_sidecar_produces_a_usable_bundle(image, tmp_path):
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    (state / "config").mkdir(parents=True)
    backups.mkdir()
    (state / "config" / "config.toml").write_text('[urls]\nmlflow = "http://mlflow:5000"\n')
    seeded = _seed(state)
    assert seeded == 5

    run = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{state}:/state", "-v", f"{backups}:/backups",
            "-e", "PLATFORM_DB=/state/platform.db",
            "-e", "EXAMLOPS_DATA_DIR=/state",
            "-e", "EXAMLOPS_BACKUP_DIR=/backups",
            "-e", "EXAMLOPS_ACTOR=backup-sidecar",
            image, "backup", "schedule", "--once", "--tiers", "sqlite,config",
        ],
        capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    print("sidecar said:", run.stdout.strip() or run.stderr.strip())
    assert run.returncode == 0, f"the sidecar cycle failed:\n{run.stdout}\n{run.stderr}"

    bundles = sorted(backups.glob("examlops-backup-*"))
    assert len(bundles) == 1, f"one cycle should leave one bundle, found {bundles}"
    bundle = bundles[0]

    manifest = json.loads((bundle / "bundle.manifest.json").read_text())
    tiers = manifest.get("tiers", {})
    sqlite_items = {i["name"]: i.get("status") for i in tiers.get("sqlite", {}).get("items", [])}
    print("sqlite tier:", json.dumps(sqlite_items))
    assert sqlite_items.get("platform") == "ok", (
        "the cycle succeeded without capturing the platform datastore — a bundle of nothing but "
        f"skipped tiers reports success just as loudly: {tiers.get('sqlite')}"
    )
    config_items = {i["name"]: i.get("status") for i in tiers.get("config", {}).get("items", [])}
    assert config_items.get("data-root:config") == "ok", (
        f"the instance's own config directory was not captured: {config_items}"
    )

    # The checksums the container wrote must be the ones the restore path checks.
    from examlops.backup.bundle import verify_bundle

    verdict = verify_bundle(str(bundle))
    assert verdict["ok"], f"the bundle the sidecar wrote does not verify: {verdict}"

    # And the copied database is a working datastore, not a file of the right size.
    copied = next((bundle / "sqlite").glob("platform-*.db"))
    restored = tmp_path / "readback.db"
    shutil.copy(copied, restored)
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'platform/cli/src')\n"
            "from examlops.data.audit import verify_audit_chain\n"
            "import json; print(json.dumps(verify_audit_chain()))\n",
        ],
        cwd=ROOT,
        env={**os.environ, "PLATFORM_DB": str(restored)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    chain = json.loads(out.stdout.strip().splitlines()[-1])
    print("chain inside the sidecar's bundle:", chain["ok"], chain["count"], "events")
    assert chain["ok"] and chain["count"] == seeded, (
        f"the datastore the sidecar copied does not carry the audit chain it was given: {chain}"
    )
