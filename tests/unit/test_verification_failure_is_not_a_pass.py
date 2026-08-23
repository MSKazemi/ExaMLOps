"""Being unable to verify an artifact is not the same as verifying it.

`verify_before_load` is the last gate before a model's bytes are served, and in `enforce` mode its
whole job is to refuse. Refusal is decided by a *comparison* — read the recorded signature, digest
the artifact on disk, compare — and both halves of that comparison can fail for reasons that have
nothing to do with the artifact being sound: the signature store can be unreachable, an artifact
path can be unreadable. When they do, the question "does this artifact match its signature?" has no
answer, and the one answer that must not be given is "yes".

The KServe loader hook made exactly that substitution: it wrapped the call in `except Exception:
return True` to keep Compose/dev usable when the supply-chain module is absent, so every failure of
the verifier — not just its absence — was reported to the loader as a clean verification.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import supplychain  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.serving_backends import KServeK8s  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-signing-key")
    init_db()
    supplychain._verify_cache.clear()


def _artifacts(tmp_path: Path) -> list[Path]:
    a = tmp_path / "model.pkl"
    a.write_bytes(b"weights-v1")
    return [a]


def _explode(*_a, **_k):
    raise OSError("signature store unreachable")


def test_enforce_refuses_when_the_verifier_cannot_run(tmp_path, monkeypatch):
    """A store it cannot read must refuse, not pass — the artifact is unverified either way."""
    monkeypatch.setattr(supplychain, "verify_model", _explode)
    assert (
        supplychain.verify_before_load("JPCP", "17", _artifacts(tmp_path), mode="enforce") is False
    )


def test_warn_still_loads_when_the_verifier_cannot_run(tmp_path, monkeypatch):
    """`warn` is documented to load anyway; a broken verifier must not turn it into a hard block."""
    monkeypatch.setattr(supplychain, "verify_model", _explode)
    assert supplychain.verify_before_load("JPCP", "17", _artifacts(tmp_path), mode="warn") is True


def test_a_failed_verification_is_recorded_even_when_it_failed_to_run(tmp_path, monkeypatch):
    """Silence is what made this invisible: refusing without a record leaves nothing to alert on."""
    from examlops.platform_db import get_db

    monkeypatch.setattr(supplychain, "verify_model", _explode)
    supplychain.verify_before_load("JPCP", "17", _artifacts(tmp_path), mode="enforce")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT action, details FROM audit_events WHERE target = 'JPCP@17'"
        ).fetchall()
    assert any("verify" in str(r[0]) for r in rows), f"no verification event recorded: {rows}"


def test_the_kserve_hook_refuses_a_verifier_that_raises(tmp_path, monkeypatch):
    """The loader hook's broad `except` reported every failure as a clean verification."""
    monkeypatch.setattr(supplychain, "verify_before_load", _explode)
    backend = KServeK8s()
    assert backend.verify_before_load("JPCP", "17", _artifacts(tmp_path), mode="enforce") is False


def test_the_kserve_hook_still_permits_when_the_module_is_absent(tmp_path, monkeypatch):
    """The exemption that earns the `except` stays: no supply-chain module, no blocking dev."""
    import builtins

    real_import = builtins.__import__

    def _no_supplychain(name, *args, **kwargs):
        if name == "examlops.supplychain":
            raise ImportError("no module named examlops.supplychain")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_supplychain)
    backend = KServeK8s()
    assert backend.verify_before_load("JPCP", "17", _artifacts(tmp_path), mode="enforce") is True
