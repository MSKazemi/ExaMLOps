# tests/unit/test_adapter_signing_degrades_honestly.py
"""An unsigned adapter must say *why* it is unsigned.

`exa finetune` signs the adapter identity with the D3 HMAC key and records the signature. Running
without a signing key is a site's choice, so the adapter is registered unsigned — that is the
documented degradation.

The trap is that a **failure** to sign used to produce the identical result. `_sign` caught
`Exception`, so a malformed key, an unreachable secret store or a bug in the signer all returned
`(None, None)` — the same value as "no key configured", recorded in the same column, with nothing
anywhere to tell them apart. A broken signer became indistinguishable from policy, permanently and
silently: exactly the shape of the governance digest that verified nothing.

So the two paths are tested separately, and the difference is the log line. The command still
succeeds either way — a signing fault should not cost someone their fine-tuning run — but it can no
longer pass unnoticed.
"""

from __future__ import annotations

import logging

import pytest

from examlops.finetuning import _sign as sign_adapter
from examlops.reproducibility import _sign as sign_bundle
from examlops.supplychain import SigningKeyMissing


@pytest.fixture(autouse=True)
def _no_signing_key(monkeypatch):
    """No key from the environment; individual tests decide what the secret store does."""
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY", raising=False)


def test_a_configured_key_signs(monkeypatch):
    """The baseline: without this, every assertion below passes on a signer that never runs."""
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k" * 32)

    signature, algo = sign_adapter("adapter-1", "base", "rev1")

    assert algo == "hmac-sha256"
    assert signature and len(signature) == 64, signature


def test_the_same_identity_signs_the_same_way(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k" * 32)

    assert sign_adapter("a", "b", "c") == sign_adapter("a", "b", "c")
    assert sign_adapter("a", "b", "c") != sign_adapter("a", "b", "different")


def test_no_signing_key_degrades_quietly(monkeypatch, caplog):
    """A site that configures no key gets an unsigned adapter and no noise about it."""
    monkeypatch.setattr(
        "examlops.supplychain._hmac_sign",
        lambda payload: (_ for _ in ()).throw(SigningKeyMissing("no signing key")),
    )

    with caplog.at_level(logging.WARNING):
        assert sign_adapter("adapter-2", "base", "rev") == (None, None)

    assert not caplog.records, (
        f"a deliberate choice must not warn: {[r.message for r in caplog.records]}"
    )


def test_a_signing_failure_is_unsigned_but_says_so(monkeypatch, caplog):
    """The case that used to be invisible: signing broke, and the record looked like policy."""
    monkeypatch.setattr(
        "examlops.supplychain._hmac_sign",
        lambda payload: (_ for _ in ()).throw(ValueError("key is not valid base64")),
    )

    with caplog.at_level(logging.WARNING):
        assert sign_adapter("adapter-3", "base", "rev") == (None, None)

    assert caplog.records, (
        "a signing failure degraded silently — indistinguishable from having no key"
    )
    message = caplog.records[0].getMessage()
    assert "adapter-3" in message
    assert "ValueError" in message and "not valid base64" in message, message
    assert "not the same as" in message, "the message must separate failure from policy"


def test_the_reproducibility_bundle_signs_the_same_way(monkeypatch, caplog):
    """The second caller of the same signer — it had the identical blanket catch.

    Both are routed through `supplychain.sign_or_explain` so the distinction cannot be fixed in one
    place and left broken in the other, which is what a copied `except Exception` guarantees.
    """
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k" * 32)
    signature, algo = sign_bundle("a" * 64)
    assert algo == "hmac-sha256" and signature

    monkeypatch.setattr(
        "examlops.supplychain._hmac_sign",
        lambda payload: (_ for _ in ()).throw(ValueError("key is not valid base64")),
    )
    with caplog.at_level(logging.WARNING):
        assert sign_bundle("a" * 64) == (None, None)
    assert caplog.records, "the bundle path degraded silently on a signing failure"
    assert "reproducibility bundle" in caplog.records[0].getMessage()
