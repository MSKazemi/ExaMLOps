"""SOPS + age encrypted-file backend — the dev/CI fallback tier of ADR 0011 clause 1.

The ADR decided on **SOPS + age** as the fallback that keeps the secrets feature working with no
running vault while never putting a plaintext secret into a committed file. This module is that
tier: an encrypted YAML/JSON/dotenv document (``EXAMLOPS_SOPS_FILE``) whose *values* are encrypted
under one or more age recipients, and which ``sops`` decrypts with the identity in
``SOPS_AGE_KEY_FILE`` / ``SOPS_AGE_KEY``. The file itself is safe to commit; the age identity is
the one secret that is not.

Design points:

* **No plaintext cache.** Each read runs ``sops decrypt --extract '["a"]["b"]'`` for exactly the
  path asked for, so the process never holds the other secrets in the file.
* **No value on a command line.** Writes use ``sops set --value-stdin`` — the value never appears
  in ``ps``.
* **Bounded.** Every call has a timeout (``EXAMLOPS_SOPS_TIMEOUT``, default 10 s) and output is
  capped at :data:`MAX_OUTPUT_BYTES`.
* **Two outcomes kept apart,** exactly as the vault client does: *"the file answered, this key is
  not in it"* (fall through quietly) versus *"sops could not answer"* (missing binary, missing
  file, wrong age identity, timeout) — only the second is a degradation and is reported.

A secret path maps onto the document's nesting: ``control-plane/token`` is
``{"control-plane": {"token": ...}}``, and a tenant-prefixed path ``acme/api-key`` is
``{"acme": {"api-key": ...}}``, so the tenant scoping in :mod:`examlops.secrets` applies unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

MAX_OUTPUT_BYTES = 1024 * 1024
_DEFAULT_TIMEOUT = 10.0
# What sops prints when --extract names a key the document does not hold. Anything else on a
# non-zero exit is an outage (no identity, corrupt MAC, missing file) and must not be mistaken for
# "not here". sops 3.x: `error truncating tree: component ['x'] not found`.
_NOT_FOUND_MARKER = "error truncating tree"


class SopsError(RuntimeError):
    """Raised when a SOPS write cannot be completed (fail closed — nothing is written elsewhere)."""


def sops_file() -> str:
    """The configured encrypted document, or ``""`` when the SOPS tier is off."""
    return os.getenv("EXAMLOPS_SOPS_FILE", "").strip()


def configured() -> bool:
    return bool(sops_file())


def sops_bin() -> str | None:
    """Path of the ``sops`` executable (``EXAMLOPS_SOPS_BIN`` overrides ``$PATH`` lookup)."""
    explicit = os.getenv("EXAMLOPS_SOPS_BIN", "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("sops")


def _timeout() -> float:
    try:
        value = float(os.getenv("EXAMLOPS_SOPS_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    except ValueError:
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def index_for(path: str) -> str:
    """``a/b-c`` → ``["a"]["b-c"]`` — the sops tree index. Rejects empty segments."""
    segments = path.split("/")
    if not path or any(not s or s in {".", ".."} for s in segments):
        raise ValueError(f"invalid secret path for the SOPS backend: {path!r}")
    return "".join(f"[{json.dumps(s)}]" for s in segments)


def _short(stderr: bytes | str) -> str:
    text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
    # sops prefixes deprecation notices with "[warning]"; they are noise in an error report.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("[warning]")]
    return " ".join(lines)[:300] or "no diagnostic output"


def _run(args: list[str], *, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell; the binary is operator-configured
        args,
        input=stdin,
        capture_output=True,
        timeout=_timeout(),
        check=False,
    )


def get(path: str) -> tuple[str | None, str | None]:
    """``(value, error)`` — ``error`` is set only when SOPS *failed to answer*.

    ``(None, None)`` means the SOPS tier is off, or the document answered and does not hold the
    key; the caller then falls through to the next backend.
    """
    target = sops_file()
    if not target:
        return None, None
    binary = sops_bin()
    if binary is None:
        return None, "sops is not installed (set EXAMLOPS_SOPS_BIN or put sops on PATH)"
    if not Path(target).is_file():
        return None, f"EXAMLOPS_SOPS_FILE does not exist: {target}"
    try:
        index = index_for(path)
    except ValueError:
        return None, None  # a path the document cannot hold is simply not in it
    try:
        proc = _run([binary, "decrypt", "--extract", index, target])
    except subprocess.TimeoutExpired:
        return None, f"sops timed out after {_timeout():g}s"
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        diag = _short(proc.stderr)
        if _NOT_FOUND_MARKER in diag and "not found" in diag:
            return None, None
        return None, f"sops exited {proc.returncode}: {diag}"
    if len(proc.stdout) > MAX_OUTPUT_BYTES:
        return None, f"sops output exceeds {MAX_OUTPUT_BYTES} bytes - refusing it"
    return proc.stdout.decode(), None


def put(path: str, value: str) -> None:
    """Set ``path`` to ``value`` in the encrypted document (re-encrypted in place by sops).

    Fails closed: any problem raises :class:`SopsError`, and nothing is written anywhere else.
    """
    target = sops_file()
    if not target:
        raise SopsError("EXAMLOPS_SOPS_FILE is not set - the SOPS backend is not configured")
    binary = sops_bin()
    if binary is None:
        raise SopsError("sops is not installed (set EXAMLOPS_SOPS_BIN or put sops on PATH)")
    if not Path(target).is_file():
        raise SopsError(
            f"EXAMLOPS_SOPS_FILE does not exist: {target} - create it once with "
            "`sops encrypt --age <recipient> plain.yaml > <file>`"
        )
    try:
        index = index_for(path)
    except ValueError as exc:
        raise SopsError(str(exc)) from exc
    try:
        proc = _run(
            [binary, "set", "--value-stdin", target, index],
            stdin=json.dumps(value).encode(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SopsError(f"sops timed out after {_timeout():g}s") from exc
    except OSError as exc:
        raise SopsError(f"{type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise SopsError(f"sops set exited {proc.returncode}: {_short(proc.stderr)}")


def status() -> dict[str, Any]:
    """Configuration and health of the SOPS tier — never a secret value."""
    target = sops_file()
    binary = sops_bin()
    out: dict[str, Any] = {
        "configured": bool(target),
        "file": target or None,
        "file_exists": bool(target) and Path(target).is_file(),
        "binary": binary,
        "identity": bool(os.getenv("SOPS_AGE_KEY_FILE") or os.getenv("SOPS_AGE_KEY")),
        "version": None,
        "error": None,
    }
    if binary is None:
        out["error"] = "sops is not installed" if target else None
        return out
    try:
        proc = _run([binary, "--version", "--disable-version-check"])
        first = proc.stdout.decode(errors="replace").strip().splitlines()
        out["version"] = first[0] if first else None
    except (subprocess.TimeoutExpired, OSError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    if target and not out["file_exists"]:
        out["error"] = f"EXAMLOPS_SOPS_FILE does not exist: {target}"
    return out
