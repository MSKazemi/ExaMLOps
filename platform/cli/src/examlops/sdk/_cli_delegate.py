"""Run one ``exa`` command in a bounded child process and read its single JSON document.

Used where a CLI command *is* the governed implementation and extracting it would duplicate its
gates (``exa pipeline promote``). The child is the same interpreter importing the same
``examlops`` package (its source root is put first on ``PYTHONPATH``), run with
``--output json --yes`` so it prints exactly one JSON document on stdout (the ``--json``
contract) and warnings as JSON lines on stderr. Private: not part of the SDK surface.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from examlops.sdk.errors import (
    GateRefusedError,
    NotFoundError,
    PolicyDeniedError,
    SDKError,
    UnavailableError,
)

#: Bytes of each child stream read back (its tail) — a runaway command cannot exhaust memory here.
MAX_OUTPUT_BYTES = 1_000_000

_UNAVAILABLE_MARKERS = ("Failed to fetch", "is MLflow running", "Connection refused", "timed out")


@dataclass(frozen=True)
class CliOutcome:
    exit_code: int
    document: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def as_error(self, message: str) -> SDKError:
        """Classify a non-zero exit by what the command said, keeping its message verbatim."""
        detail = {"exit_code": self.exit_code, "hint": self.document.get("hint")}
        if message.startswith(("Policy denied", "Policy gate denied", "plan_required")):
            return PolicyDeniedError(message, **detail)
        # Case-folded: the transport's 404 reads "Not found: <url>" inside "Failed to fetch …",
        # which must be a missing model, not an outage the caller would retry.
        if message.startswith("No version found") or "not found" in message.casefold():
            return NotFoundError(message, **detail)
        if any(m in message for m in _UNAVAILABLE_MARKERS):
            return UnavailableError(message, **detail)
        return GateRefusedError(message, **detail)


def _source_root() -> str:
    import examlops

    return str(Path(examlops.__file__).resolve().parents[1])


def _last_document(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the last line that parses — never guess beyond that.
        for line in reversed(text.splitlines()):
            try:
                doc = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        else:
            return {"message": text[-2000:]}
    return doc if isinstance(doc, dict) else {"result": doc}


def _warnings(stderr: str) -> list[str]:
    out: list[str] = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict) and "warning" in doc:
            out.append(str(doc["warning"]))
    return out


def _tail(fh: Any) -> str:
    """The last :data:`MAX_OUTPUT_BYTES` of a spooled output file, decoded."""
    fh.seek(0, os.SEEK_END)
    fh.seek(max(0, fh.tell() - MAX_OUTPUT_BYTES))
    return fh.read().decode("utf-8", errors="replace")


def run_cli(argv: list[str], *, timeout: float) -> CliOutcome:
    """Run ``exa --output json --yes <argv>``; raise :class:`UnavailableError` on a timeout.

    The child's output goes to temporary files, not pipes, and only the last
    :data:`MAX_OUTPUT_BYTES` of each is read back — so this process's memory is bounded whatever
    the child prints. ``subprocess.run`` kills the child when ``timeout`` expires.
    """
    env = dict(os.environ)
    root = _source_root()
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH", "")) if p)
    cmd = [sys.executable, "-m", "examlops.cli", "--output", "json", "--yes", *argv]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed interpreter + validated argv, no shell
                cmd,
                stdout=out,
                stderr=err,
                timeout=timeout,
                env=env,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UnavailableError(
                f"`exa {' '.join(argv[:2])}` timed out after {timeout:g}s"
            ) from exc
        stdout, stderr = _tail(out), _tail(err)
    return CliOutcome(
        exit_code=int(proc.returncode),
        document=_last_document(stdout),
        warnings=_warnings(stderr),
    )
