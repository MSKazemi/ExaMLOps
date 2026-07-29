"""A7 R5/GWT-5 — synthetic-only promotion gate (ADR 0042, BL-008).

Proves that a model trained only on synthetic data is detectable from its A2 lineage, so the
manual promote path (env gate) and the autopilot (policy context) can refuse to promote it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def _record_revision(revision_id: str, *, synthetic: bool, backend: str = "minio") -> None:
    from examlops.data.data_assets import record_dataset_revision
    from pipelines.datasets.versioning import DatasetRevision

    rev = DatasetRevision(
        backend=backend,
        dataset="FData",
        revision_id=revision_id,
        kind="synthetic" if synthetic else "content",
    )
    record_dataset_revision(
        rev, synthetic=synthetic, generator="gaussian_copula" if synthetic else None
    )


def _train_with(model: str, run_id: str, revision_id: str) -> None:
    from examlops.data.events import record_lineage_event

    record_lineage_event(
        run_id,
        job=f"train:{model}",
        event_type="COMPLETE",
        model=model,
        dataset_revision=revision_id,
    )


def test_synthetic_only_true_when_all_training_revisions_synthetic():
    from examlops.promotion_gates import synthetic_only_training

    _record_revision("syn1", synthetic=True)
    _train_with("JPCP", "run-1", "syn1")

    only_synth, revs = synthetic_only_training("JPCP")
    assert only_synth is True
    assert revs == ["syn1"]


def test_synthetic_only_false_when_any_real_revision_present():
    from examlops.promotion_gates import synthetic_only_training

    _record_revision("syn1", synthetic=True)
    _record_revision("real1", synthetic=False)
    _train_with("JPCP", "run-1", "syn1")
    _train_with("JPCP", "run-2", "real1")

    only_synth, revs = synthetic_only_training("JPCP")
    assert only_synth is False
    assert set(revs) == {"syn1", "real1"}


def test_no_lineage_fails_open_not_synthetic_only():
    from examlops.promotion_gates import synthetic_only_training

    only_synth, revs = synthetic_only_training("UNKNOWN")
    assert only_synth is False  # unknown provenance never blocks a promotion
    assert revs == []


def test_training_dataset_revisions_dedupes():
    from examlops.promotion_gates import training_dataset_revisions

    _record_revision("syn1", synthetic=True)
    _train_with("JPCP", "run-1", "syn1")
    _train_with("JPCP", "run-2", "syn1")  # same revision, second run
    assert training_dataset_revisions("JPCP") == ["syn1"]


def test_gate_enabled_reads_env(monkeypatch):
    from examlops.promotion_gates import synthetic_only_gate_enabled

    monkeypatch.delenv("EXAMLOPS_SYNTHETIC_ONLY_GATE", raising=False)
    assert synthetic_only_gate_enabled() is False
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("EXAMLOPS_SYNTHETIC_ONLY_GATE", truthy)
        assert synthetic_only_gate_enabled() is True
    monkeypatch.setenv("EXAMLOPS_SYNTHETIC_ONLY_GATE", "0")
    assert synthetic_only_gate_enabled() is False


def test_policy_can_forbid_synthetic_only_promotion():
    """A D5 policy rule keyed on synthetic_only denies auto-promotion (spec R5 mechanism)."""
    from examlops import policy

    rules = [
        {
            "name": "no-synthetic-only",
            "action": "autopilot_promote",
            "when": "synthetic_only == True",
            "effect": "deny",
        }
    ]
    denied = policy.decide(
        "autopilot_promote", {"model": "JPCP", "synthetic_only": True}, policies=rules, audit=False
    )
    assert denied.denied is True

    allowed = policy.decide(
        "autopilot_promote", {"model": "JPCP", "synthetic_only": False}, policies=rules, audit=False
    )
    assert allowed.denied is False
