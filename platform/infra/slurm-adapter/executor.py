"""
Transport layer for HPC scheduler adapters — orthogonal to the scheduler backend.

An adapter (Slurm or Flux) is handed a ``RemoteExecutor`` and uses it to run scheduler
CLIs and stage files. Two implementations:

  * ``LocalExecutor`` — ``subprocess`` + ``shutil.copy``; reproduces the historical
    "worker runs on the login node with a shared filesystem" behavior.
  * ``SSHExecutor``   — paramiko SSH + SFTP; lets a Docker Prefect worker submit to a
    remote login node (e.g. lxp) with no shared filesystem.

paramiko is chosen over asyncssh (the adapter contract and Prefect tasks are synchronous;
an event loop would defeat connection reuse and complicate the long poll loop) and over
fabric (extra Invoke/config layer; direct paramiko gives explicit keepalive, SFTP, and a
clean single-reconnect hook — exactly the two failure modes that matter: long-poll drops
and file staging).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from scheduler import _CMD_TIMEOUT, JobTimeoutError


@dataclass
class CompletedCommand:
    """Result of a remote/local command execution."""

    returncode: int
    stdout: str
    stderr: str


@runtime_checkable
class RemoteExecutor(Protocol):
    """Transport abstraction: run commands and move files, locally or over SSH."""

    def run(
        self, cmd: list[str], *, timeout: float | None = None, cwd: str | None = None
    ) -> CompletedCommand: ...

    def put(self, local: str, remote: str) -> None:
        """Stage a local file up to the (possibly remote) target path."""
        ...

    def get(self, remote: str, local: str) -> None:
        """Fetch a (possibly remote) file down to a local path."""
        ...

    def close(self) -> None: ...


class LocalExecutor:
    """Runs commands via ``subprocess`` and copies files on the local filesystem."""

    def run(
        self, cmd: list[str], *, timeout: float | None = None, cwd: str | None = None
    ) -> CompletedCommand:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else _CMD_TIMEOUT,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise JobTimeoutError(f"{cmd[0]} timed out after {timeout or _CMD_TIMEOUT}s") from exc
        return CompletedCommand(proc.returncode, proc.stdout, proc.stderr)

    def put(self, local: str, remote: str) -> None:
        Path(remote).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(local, remote)

    def get(self, remote: str, local: str) -> None:
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(remote, local)

    def close(self) -> None:  # noqa: D401 - nothing to release
        return None


class SSHExecutor:
    """Runs commands and moves files on a remote host over a reused SSH connection.

    Credentials come from the environment (see ``get_executor``). The connection and one
    SFTP client are created lazily and cached; a dropped connection is transparently
    reconnected once per call (covers the hours-long ``wait_until_complete`` poll loop).
    """

    def __init__(
        self,
        host: str,
        user: str | None = None,
        key_path: str | None = None,
        port: int = 22,
        connect_timeout: int = 15,
        keepalive: int = 30,
    ):
        self.host = host
        self.user = user
        self.key_path = key_path
        self.port = port
        self.connect_timeout = connect_timeout
        self.keepalive = keepalive
        self._client = None  # type: ignore[var-annotated]
        self._sftp = None  # type: ignore[var-annotated]

    # ── connection management ────────────────────────────────────────────────

    def _connect(self):
        import paramiko  # noqa: PLC0415 - optional dep, only needed for SSH transport

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            key_filename=self.key_path,
            timeout=self.connect_timeout,
            allow_agent=True,
            look_for_keys=True,
        )
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(self.keepalive)
        return client

    def _ensure_client(self):
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _reset(self) -> None:
        """Drop the cached connection so the next call reconnects fresh."""
        for handle in (self._sftp, self._client):
            try:
                if handle is not None:
                    handle.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._sftp = None
        self._client = None

    def _ensure_sftp(self):
        if self._sftp is None:
            self._sftp = self._ensure_client().open_sftp()
        return self._sftp

    # ── RemoteExecutor interface ─────────────────────────────────────────────

    def run(
        self, cmd: list[str], *, timeout: float | None = None, cwd: str | None = None
    ) -> CompletedCommand:

        import paramiko  # noqa: PLC0415

        command = " ".join(shlex.quote(part) for part in cmd)
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        eff_timeout = timeout if timeout is not None else _CMD_TIMEOUT

        for attempt in (1, 2):  # reconnect-once on a dropped connection
            try:
                client = self._ensure_client()
                _stdin, stdout, stderr = client.exec_command(command, timeout=eff_timeout)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                rc = stdout.channel.recv_exit_status()
                return CompletedCommand(rc, out, err)
            except TimeoutError as exc:
                raise JobTimeoutError(f"{cmd[0]} timed out after {eff_timeout}s over SSH") from exc
            except (paramiko.SSHException, OSError):
                self._reset()
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def put(self, local: str, remote: str) -> None:
        self._mkdir_p(str(Path(remote).parent))
        self._sftp_op(lambda sftp: sftp.put(local, remote))

    def get(self, remote: str, local: str) -> None:
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        self._sftp_op(lambda sftp: sftp.get(remote, local))

    def _sftp_op(self, op) -> None:
        import paramiko  # noqa: PLC0415

        for attempt in (1, 2):
            try:
                op(self._ensure_sftp())
                return
            except (paramiko.SSHException, OSError):
                self._reset()
                if attempt == 2:
                    raise

    def _mkdir_p(self, remote_dir: str) -> None:
        # SFTP has no recursive mkdir; shell out (cheap and reliable).
        self.run(["mkdir", "-p", remote_dir])

    def close(self) -> None:
        self._reset()


def get_executor() -> RemoteExecutor:
    """Build the transport from the environment.

    ``EXAMLOPS_HPC_TRANSPORT`` = ``local`` | ``ssh``. Default: ``ssh`` when
    ``EXAMLOPS_HPC_SSH_HOST`` is set, otherwise ``local``.
    """
    host = os.getenv("EXAMLOPS_HPC_SSH_HOST")
    transport = os.getenv("EXAMLOPS_HPC_TRANSPORT", "ssh" if host else "local").lower().strip()

    if transport == "ssh":
        if not host:
            raise ValueError("EXAMLOPS_HPC_TRANSPORT=ssh requires EXAMLOPS_HPC_SSH_HOST")
        return SSHExecutor(
            host=host,
            user=os.getenv("EXAMLOPS_HPC_SSH_USER"),
            key_path=os.getenv("EXAMLOPS_HPC_SSH_KEY"),
            port=int(os.getenv("EXAMLOPS_HPC_SSH_PORT", "22")),
        )
    return LocalExecutor()
