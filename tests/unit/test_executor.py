"""Unit tests for the transport layer (LocalExecutor + SSHExecutor factory/behavior)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ADAPTER_DIR = Path(__file__).resolve().parents[2] / "platform" / "infra" / "slurm-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

import executor as executor_mod  # noqa: E402
from executor import LocalExecutor, SSHExecutor, get_executor  # noqa: E402
from scheduler import JobTimeoutError  # noqa: E402


def test_local_executor_runs_and_captures():
    r = LocalExecutor().run(["echo", "hello"])
    assert r.returncode == 0
    assert "hello" in r.stdout


def test_local_executor_copies_files(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("data")
    dst = tmp_path / "sub" / "b.txt"
    LocalExecutor().put(str(src), str(dst))
    assert dst.read_text() == "data"
    back = tmp_path / "c.txt"
    LocalExecutor().get(str(dst), str(back))
    assert back.read_text() == "data"


def test_local_executor_timeout_becomes_job_timeout(monkeypatch):
    def _hang(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="sleep", timeout=1)

    monkeypatch.setattr(executor_mod.subprocess, "run", _hang)
    with pytest.raises(JobTimeoutError):
        LocalExecutor().run(["sleep", "100"])


def test_get_executor_defaults_local(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_HPC_SSH_HOST", raising=False)
    monkeypatch.delenv("EXAMLOPS_HPC_TRANSPORT", raising=False)
    assert isinstance(get_executor(), LocalExecutor)


def test_get_executor_ssh_requires_host(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_HPC_TRANSPORT", "ssh")
    monkeypatch.delenv("EXAMLOPS_HPC_SSH_HOST", raising=False)
    with pytest.raises(ValueError, match="EXAMLOPS_HPC_SSH_HOST"):
        get_executor()


def test_get_executor_builds_ssh_from_env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_HPC_SSH_HOST", "lxp-login")
    monkeypatch.setenv("EXAMLOPS_HPC_SSH_USER", "mohsen")
    monkeypatch.setenv("EXAMLOPS_HPC_SSH_PORT", "2222")
    ex = get_executor()
    assert isinstance(ex, SSHExecutor)
    assert ex.host == "lxp-login" and ex.user == "mohsen" and ex.port == 2222


class _FakeChannel:
    def __init__(self, rc):
        self._rc = rc

    def recv_exit_status(self):
        return self._rc


class _FakeStd:
    def __init__(self, data=b"", rc=0):
        self._data = data
        self.channel = _FakeChannel(rc)

    def read(self):
        return self._data


class _FakeClient:
    def __init__(self):
        self.commands: list[str] = []

    def set_missing_host_key_policy(self, _p):
        pass

    def connect(self, **_kw):
        pass

    def get_transport(self):
        return None

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        return _FakeStd(), _FakeStd(b"out\n", 0), _FakeStd(b"", 0)

    def close(self):
        pass


def test_ssh_run_quotes_argv(monkeypatch):
    fake = _FakeClient()
    ex = SSHExecutor(host="h")
    monkeypatch.setattr(ex, "_connect", lambda: fake)
    r = ex.run(["flux", "jobs", "-o", "{state} {result}", "ƒAbC"])
    assert r.returncode == 0 and r.stdout == "out\n"
    # F58 id and the format string with spaces must be shell-quoted into one command.
    assert "'{state} {result}'" in fake.commands[0]
    assert "ƒAbC" in fake.commands[0]


def test_ssh_run_reconnects_once_then_succeeds(monkeypatch):
    import paramiko

    calls = {"connect": 0}
    good = _FakeClient()

    class _FlakyClient(_FakeClient):
        def exec_command(self, command, timeout=None):
            raise paramiko.SSHException("dropped")

    def _connect():
        calls["connect"] += 1
        return _FlakyClient() if calls["connect"] == 1 else good

    ex = SSHExecutor(host="h")
    monkeypatch.setattr(ex, "_connect", _connect)
    # First client drops (SSHException) → reset + reconnect once → second succeeds.
    r = ex.run(["echo", "hi"])
    assert r.stdout == "out\n"
    assert calls["connect"] == 2  # exactly one reconnect


def test_ssh_run_raises_when_both_attempts_drop(monkeypatch):
    import paramiko

    calls = {"connect": 0}

    class _FlakyClient(_FakeClient):
        def exec_command(self, command, timeout=None):
            raise paramiko.SSHException("dropped")

    def _connect():
        calls["connect"] += 1
        return _FlakyClient()

    ex = SSHExecutor(host="h")
    monkeypatch.setattr(ex, "_connect", _connect)
    with pytest.raises(paramiko.SSHException):
        ex.run(["echo", "hi"])
    assert calls["connect"] == 2  # tried twice, then gave up
