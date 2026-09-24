"""ADR 0038 clause 2 — *verify* the recorded container image digest, rather than only record it.

``build_bundle`` accepts an ``image_digest`` and stores it. Until now nothing ever looked at it:
``exa reproduce run --execute`` printed "image digest recorded but not verifiable here", which is
a comment, not a check. This module asks the local container runtime what it actually holds and
reports one of five statuses, using the platform's existing resolution vocabulary (compare
:mod:`examlops.hardware_profiles`, ADR 0109/0157 — ``unchecked`` / ``verified`` / ``degraded``):

``unchecked``
    The bundle recorded no digest. Nothing was claimed, so nothing is asserted.
``verified``
    The recorded image is present locally and its digest matches.
``mismatch``
    A local image answers to the recorded *reference* but carries a different digest — the image
    that would run today is **not** the image that was recorded. Fails the ``env`` step.
``absent``
    The runtime answered, and the recorded image is not present. The recorded environment cannot
    be reconstituted, so this fails the ``env`` step too (``--allow-env-drift`` downgrades both
    to a reported, non-fatal drift).
``unverifiable``
    There is no reachable Docker daemon (no binary, daemon down, permission denied, timeout).
    The check could not run. This is **not** treated as success: it is carried in the step
    detail, in ``ExecuteResult.image_digest_status`` and in ``--json`` so a reader can see that
    the digest clause went unchecked — it just cannot fail a run for a missing local tool.

A recorded value of the form ``repo[:tag]@sha256:…`` is the one that can genuinely mismatch: the
*name* is inspected and the digest it resolves to today is compared with the recorded one. A bare
``sha256:…`` can only be present or absent, because inspecting by digest is self-answering.

Everything here is read-only (``docker version``, ``docker image inspect``); no image is pulled,
built, run or removed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

STATUS_UNCHECKED = "unchecked"
STATUS_VERIFIED = "verified"
STATUS_MISMATCH = "mismatch"
STATUS_ABSENT = "absent"
STATUS_UNVERIFIABLE = "unverifiable"

#: Statuses that mean the recorded environment is demonstrably not reconstitutable here.
FAILING_STATUSES = frozenset({STATUS_MISMATCH, STATUS_ABSENT})

_DIGEST = re.compile(r"^[a-z0-9]+:[0-9a-f]{32,}$")

Runner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]


@dataclass
class DigestCheck:
    status: str
    detail: str
    recorded: str | None = None
    #: Digests the local runtime reports for the inspected reference.
    local: list[str] = field(default_factory=list)

    @property
    def failing(self) -> bool:
        return self.status in FAILING_STATUSES


def _run(args: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def split_reference(recorded: str) -> tuple[str | None, str | None]:
    """``repo[:tag]@sha256:…`` -> ``(name, digest)``; a bare digest -> ``(None, digest)``."""
    value = recorded.strip()
    if "@" in value:
        name, _, digest = value.rpartition("@")
        return (name or None), (digest or None)
    if _DIGEST.match(value):
        return None, value
    return value or None, None


def verify_image_digest(
    recorded: str | None,
    *,
    runner: Runner | None = None,
    timeout: int = 30,
    docker_bin: str | None = None,
) -> DigestCheck:
    """Check ``recorded`` against the image the local container runtime actually holds."""
    if not recorded:
        return DigestCheck(STATUS_UNCHECKED, "bundle recorded no container image digest")
    run = runner or _run
    docker = docker_bin or shutil.which("docker")
    if not docker:
        return DigestCheck(
            STATUS_UNVERIFIABLE,
            "no docker binary on PATH — the recorded image digest went unchecked",
            recorded=recorded,
        )
    try:
        alive = run([docker, "version", "--format", "{{.Server.Version}}"], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return DigestCheck(
            STATUS_UNVERIFIABLE, f"docker is not usable here: {exc}", recorded=recorded
        )
    if alive.returncode != 0:
        why = (alive.stderr or alive.stdout).strip().splitlines()[-1:] or [""]
        return DigestCheck(
            STATUS_UNVERIFIABLE,
            f"no reachable docker daemon ({why[0][:160]}) — image digest went unchecked",
            recorded=recorded,
        )

    name, digest = split_reference(recorded)
    ref = name or digest or recorded
    try:
        got = run(
            [docker, "image", "inspect", "--format", "{{.Id}}|{{json .RepoDigests}}", ref], timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DigestCheck(
            STATUS_UNVERIFIABLE, f"docker image inspect failed to run: {exc}", recorded=recorded
        )
    if got.returncode != 0:
        text = (got.stderr or got.stdout).strip()
        if "no such image" in text.lower():
            return DigestCheck(
                STATUS_ABSENT,
                f"recorded image {ref} is not present locally — the recorded environment "
                "cannot be reconstituted here",
                recorded=recorded,
            )
        return DigestCheck(
            STATUS_UNVERIFIABLE,
            f"docker could not inspect {ref}: {text.splitlines()[-1:][0][:160] if text else '?'}",
            recorded=recorded,
        )

    local = _local_digests(got.stdout)
    if not digest:
        return DigestCheck(
            STATUS_VERIFIED,
            f"image {ref} is present locally (bundle recorded no digest to compare)",
            recorded=recorded,
            local=local,
        )
    if digest in local or recorded.strip() in local:
        return DigestCheck(
            STATUS_VERIFIED,
            f"{ref} matches the recorded digest {digest[:19]}…",
            recorded=recorded,
            local=local,
        )
    return DigestCheck(
        STATUS_MISMATCH,
        f"{ref} resolves to {', '.join(d[:19] + '…' for d in local) or '(no digest)'} locally, "
        f"but the bundle recorded {digest[:19]}…",
        recorded=recorded,
        local=local,
    )


def _local_digests(stdout: str) -> list[str]:
    """Every digest the inspected image answers to: its config id and its repo digests."""
    head, _, tail = (stdout or "").strip().partition("|")
    out: list[str] = []
    if head.strip():
        out.append(head.strip())
    try:
        repo_digests = json.loads(tail) if tail.strip() else []
    except ValueError:
        repo_digests = []
    for entry in repo_digests or []:
        if not isinstance(entry, str):
            continue
        out.append(entry)
        if "@" in entry:
            out.append(entry.rpartition("@")[2])
    return [d for d in dict.fromkeys(out) if d]


__all__ = [
    "FAILING_STATUSES",
    "STATUS_ABSENT",
    "STATUS_MISMATCH",
    "STATUS_UNCHECKED",
    "STATUS_UNVERIFIABLE",
    "STATUS_VERIFIED",
    "DigestCheck",
    "split_reference",
    "verify_image_digest",
]
