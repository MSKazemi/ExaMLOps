"""The verification cache answered a different question than the gate asked.

`verify_before_load` is the last thing standing between a set of bytes and a served model, and
`verify_model` is where it gets its answer. That answer was cached — spec D3 R8, "verification
results SHOULD be cached per artifact digest" — and per *digest* is the whole defect: it records
"these bytes verified once, for something" while the caller asked "do these bytes match the
signature recorded for **this** model version?". Two records that share a digest make those
different questions, and the difference is a false accept, in `enforce` mode, on the load path.

The suite could not see it. Every one of the nine tests that touched the cache began by clearing
it, so the only behaviour ever exercised was a cold cache and a repeat of the identical question.
These tests deliberately do not clear anything: the carry-over between calls is the subject.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import supplychain  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-signing-key")
    init_db()


def _artifacts(d: Path, body: bytes) -> list[Path]:
    d.mkdir(parents=True, exist_ok=True)
    a = d / "model.pkl"
    a.write_bytes(body)
    b = d / "MLmodel"
    b.write_text("flavor: sklearn\n")
    return [a, b]


def test_one_models_verified_bytes_do_not_vouch_for_another_models_artifacts(tmp_path):
    """The failure this file exists for, in the order it happens in a serving process.

    Model A is signed and loaded, so its digest is now a digest that "has verified". Model B's
    artifacts are then replaced with A's bytes — the classic swap, and exactly what the digest
    comparison is there to catch. With a digest-keyed cache the comparison never ran.
    """
    a = _artifacts(tmp_path / "A", b"weights-A")
    supplychain.sign_model("ModelA", "1", a)
    assert supplychain.verify_model("ModelA", "1", a).ok is True

    b = _artifacts(tmp_path / "B", b"weights-B")
    supplychain.sign_model("ModelB", "1", b)
    (tmp_path / "B" / "model.pkl").write_bytes(b"weights-A")

    result = supplychain.verify_model("ModelB", "1", b)
    assert result.ok is False, "B's artifacts do not match B's recorded digest"
    assert "tamper" in result.reason, f"reported {result.reason!r} instead of naming the tamper"


def test_the_load_gate_refuses_that_swap(tmp_path):
    """The same swap through the function serving actually calls, in the mode that must refuse."""
    a = _artifacts(tmp_path / "A", b"weights-A")
    supplychain.sign_model("ModelA", "1", a)
    supplychain.verify_model("ModelA", "1", a)

    b = _artifacts(tmp_path / "B", b"weights-B")
    supplychain.sign_model("ModelB", "1", b)
    (tmp_path / "B" / "model.pkl").write_bytes(b"weights-A")

    assert supplychain.verify_before_load("ModelB", "1", b, mode="enforce") is False


def test_a_tamper_after_a_good_verification_of_the_same_version_is_still_caught(tmp_path):
    """The single-model form: a warm answer must not outlive the bytes it was about."""
    paths = _artifacts(tmp_path / "M", b"weights-1")
    supplychain.sign_model("M", "1", paths)
    assert supplychain.verify_model("M", "1", paths).ok is True

    paths[0].write_bytes(b"weights-EVIL")
    assert supplychain.verify_model("M", "1", paths).ok is False
    assert supplychain.verify_before_load("M", "1", paths, mode="enforce") is False


def test_a_rotated_signing_key_is_not_answered_from_an_older_verdict(tmp_path, monkeypatch):
    """The second way a cached verdict goes stale: the answer depends on the key, too.

    Nothing in a `(signature, digest)` pair changes when the signing key rotates, so a cache keyed
    on it would keep returning the verdict computed under the old key. Here the recorded signature
    is the old key's; under the new key it must not verify.
    """
    paths = _artifacts(tmp_path / "M", b"weights-1")
    supplychain.sign_model("M", "1", paths)
    assert supplychain.verify_model("M", "1", paths).ok is True

    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "a-different-signing-key")
    result = supplychain.verify_model("M", "1", paths)
    assert result.ok is False
    assert result.reason == "bad-signature"


def test_the_module_keeps_no_verification_state_between_calls(tmp_path):
    """A guard on the shape, not the symptom: no module-level cache may come back unnoticed.

    Re-keying the cache correctly is possible and was rejected — the key would have to carry the
    signing key, which costs more to resolve than the 4 microseconds of HMAC it would save. If a
    future change reintroduces one, this fails and the four tests above become the questions it
    has to answer.
    """
    caches = [
        name
        for name, value in vars(supplychain).items()
        if isinstance(value, dict) and name.startswith("_") and "cache" in name.lower()
    ]
    assert caches == [], (
        f"supplychain grew a module-level verification cache again: {caches}. Every cached verdict "
        "about a signature is stale as soon as the artifacts, the record or the key change."
    )
