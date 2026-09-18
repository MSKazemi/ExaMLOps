# tests/unit/test_provider_fallback_is_visible.py
"""A site's configured provider failing is not the same as configuring none.

`examlops.providers` lets a site swap the calculation behind drift, promotion, placement, carbon and
cost (ADR 0074), and every resolver falls back to the built-in default when anything goes wrong.
The fallback itself is right: a broken plugin must not stop a promotion or a cost report.

What was wrong is that it was **silent**, and identical to the ordinary case of having configured
nothing at all. A site that had deliberately installed a stricter promotion gate, its own placement
score or a different carbon coefficient simply got the platform's answer instead — no log, no
record, no way to tell from the result. The docstring of the promotion resolver even promised that
promotion "never silently breaks"; it did not break, it silently *substituted*.

Promotion gets the stronger treatment because it is a **gate**: the fallback cause is appended to
the verdict's own reason, so whoever reads the promotion record sees it without going to the logs.
"""

from __future__ import annotations

import logging

import pytest


class _Boom:
    """A provider that resolves fine and then fails when asked to compute."""

    name = "site-custom"

    def compute(self, inputs):  # noqa: ANN001, ANN201 - matches the Provider protocol
        raise ValueError("coefficient table is empty")


def test_a_healthy_provider_is_used_and_says_nothing(caplog):
    """The baseline: without it, every assertion below could pass on a resolver that never runs."""
    from examlops.promotion_providers import resolve_promotion_eval_fn

    with caplog.at_level(logging.WARNING):
        passes, reason = resolve_promotion_eval_fn()(1.0, 5.0, "lt")

    assert passes is True
    assert "built-in threshold used" not in reason, reason
    assert not caplog.records, [r.getMessage() for r in caplog.records]


def test_a_broken_provider_still_promotes_but_the_verdict_says_it_fell_back(monkeypatch, caplog):
    """The gate must not block on a broken plugin — and must not hide that it did not use it."""
    import examlops.promotion_providers as pp

    monkeypatch.setattr(pp, "resolve_provider", lambda *a, **k: _Boom())

    with caplog.at_level(logging.WARNING):
        passes, reason = pp.resolve_promotion_eval_fn()(1.0, 5.0, "lt")

    assert passes is True, "a broken provider must not block a promotion that passes the default"
    assert "configured provider failed" in reason, (
        f"the promotion record shows {reason!r} — a reader cannot tell this verdict came from the "
        "platform's default rather than the gate this site configured"
    )
    assert "coefficient table is empty" in reason, reason
    assert caplog.records, "nothing was logged when a configured provider failed"


def test_a_provider_that_cannot_be_resolved_is_reported(monkeypatch, caplog):
    """Resolution failure — a missing plugin, bad YAML — is the other half of the same event."""
    import examlops.promotion_providers as pp

    def _raise(*a, **k):
        raise ModuleNotFoundError("no module named 'site_promotion_plugin'")

    monkeypatch.setattr(pp, "resolve_provider", _raise)

    with caplog.at_level(logging.WARNING):
        _, reason = pp.resolve_promotion_eval_fn()(1.0, 5.0, "lt")

    assert "site_promotion_plugin" in reason, reason
    assert any("falling back to the built-in default" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("module", "resolver"),
    [
        ("examlops.drift_providers", "resolve_drift_score_fn"),
        ("examlops.hpc_placement_providers", "resolve_placement_score_fn"),
    ],
)
def test_every_resolver_reports_a_failed_provider(module, resolver, monkeypatch, caplog):
    """The calculation domains log rather than annotate — they have no verdict to carry a note."""
    import importlib

    mod = importlib.import_module(module)

    def _raise(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(mod, "resolve_provider", _raise)

    with caplog.at_level(logging.WARNING):
        getattr(mod, resolver)()

    assert any("falling back to the built-in default" in r.getMessage() for r in caplog.records), (
        f"{module}.{resolver} swapped in the platform default without saying so"
    )


def test_the_message_separates_failure_from_having_configured_nothing(caplog):
    from examlops.providers.loader import degraded_to_default

    with caplog.at_level(logging.WARNING):
        degraded_to_default("carbon", ValueError("bad coefficient"), configured="ccf-like")

    message = caplog.records[0].getMessage()
    assert "ccf-like" in message and "bad coefficient" in message
    assert "NOT the same as configuring no provider" in message, message
