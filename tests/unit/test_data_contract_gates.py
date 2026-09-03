# tests/unit/test_data_contract_gates.py
"""ADR 0005 clause 2 — the two enforcement points, which had no surface.

The ADR's status recorded the gap precisely: "the ADR's central value — clause 2's **two
enforcement points** — has no surface: nothing outside the CLI calls `load_contract`, so there is
no training gate before a pinned dataset trains and no inference gate in
`InferencePipelineIngress`."

The second half was the sharper case. `validate_request` shipped *for this ingress* — its
docstring reads "so the ingress can return a 4xx instead of a 5xx" — and only its own tests called
it, while the ingress hand-rolled a two-field presence check that could not see an embedding of
the wrong width.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipelines"))

pd = pytest.importorskip("pandas")

from pipelines.contracts import (  # noqa: E402
    DataContract,
    column_present,
    embedding_dim,
    min_rows,
    register_contract,
)

# ── the training gate ─────────────────────────────────────────────────────────


@pytest.fixture
def gate():
    import pipeline_generator

    return pipeline_generator


@pytest.fixture
def parquet(tmp_path):
    """A small on-disk dataset the revision resolver can point at."""
    path = tmp_path / "data"
    path.mkdir()
    pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]}).to_parquet(path / "part.parquet")
    return path


@pytest.fixture
def pinned(monkeypatch, parquet):
    """Make the A1 resolver report the local parquet as this run's pinned data."""

    class _Rev:
        uri = str(parquet)
        revision_id = "r1"
        backend = "local"

    monkeypatch.setattr("pipelines.datasets.versioning.resolve_revision", lambda *a, **k: _Rev())
    return parquet


def _contract(dataset: str, *checks) -> None:
    register_contract(DataContract(dataset=dataset, version="1", checks=list(checks)))


def test_a_conforming_dataset_passes_the_gate(gate, pinned, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATA_CONTRACT_GATE", raising=False)
    _contract("GoodDS", column_present("a"), min_rows(1))

    report = gate.data_contract_gate("GoodDS", None)

    assert report["validated"] is True
    assert report["passed"] is True


def test_an_error_severity_violation_fails_closed(gate, pinned, monkeypatch):
    """ "Fails closed on error-severity checks" is the clause's own wording."""
    monkeypatch.delenv("EXAMLOPS_DATA_CONTRACT_GATE", raising=False)
    _contract("BadDS", column_present("missing_column"))

    with pytest.raises(gate.DataContractViolation, match="violates its data contract"):
        gate.data_contract_gate("BadDS", None)


def test_warn_mode_records_the_failure_and_continues(gate, pinned, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATA_CONTRACT_GATE", "warn")
    _contract("WarnDS", column_present("missing_column"))

    report = gate.data_contract_gate("WarnDS", None)

    assert report["validated"] is True
    assert report["passed"] is False  # recorded, not raised


def test_the_gate_can_be_switched_off(gate, pinned, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATA_CONTRACT_GATE", "off")
    _contract("OffDS", column_present("missing_column"))

    assert gate.data_contract_gate("OffDS", None)["reason"] == "gate disabled"


def test_an_unrecognised_mode_falls_back_to_enforce(gate, pinned, monkeypatch):
    """A typo must not quietly disable a gate that fails closed by design."""
    monkeypatch.setenv("EXAMLOPS_DATA_CONTRACT_GATE", "enfroce")
    _contract("TypoDS", column_present("missing_column"))

    with pytest.raises(gate.DataContractViolation):
        gate.data_contract_gate("TypoDS", None)


# ── the three things that are not violations ──────────────────────────────────


def test_a_dataset_with_no_contract_is_skipped_with_a_reason(gate, pinned, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATA_CONTRACT_GATE", raising=False)
    report = gate.data_contract_gate("DatasetWithNoContract", None)
    assert report["validated"] is False
    assert "no contract" in report["reason"]


def test_a_dummy_run_is_skipped_with_a_reason(gate, pinned, monkeypatch):
    """Synthetic rows were never meant to satisfy a production contract."""
    monkeypatch.delenv("EXAMLOPS_DATA_CONTRACT_GATE", raising=False)
    _contract("DummyDS", column_present("missing_column"))

    report = gate.data_contract_gate("DummyDS", None, is_dummy=True)

    assert report["validated"] is False
    assert "dummy" in report["reason"]


def test_an_unreadable_location_is_skipped_with_a_reason(gate, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATA_CONTRACT_GATE", raising=False)

    class _Remote:
        uri = "s3://bucket/data"

    monkeypatch.setattr("pipelines.datasets.versioning.resolve_revision", lambda *a, **k: _Remote())
    _contract("RemoteDS", column_present("a"))

    report = gate.data_contract_gate("RemoteDS", None)

    assert report["validated"] is False
    assert "not a readable local path" in report["reason"]


def test_a_broken_gate_never_masquerades_as_a_pass(gate, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATA_CONTRACT_GATE", raising=False)
    monkeypatch.setattr(
        "pipelines.datasets.versioning.resolve_revision",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("resolver down")),
    )
    _contract("BrokenDS", column_present("a"))

    report = gate.data_contract_gate("BrokenDS", None)

    assert report["validated"] is False
    assert "gate error" in report["reason"]


def test_every_skip_carries_a_reason(gate, pinned, monkeypatch):
    """A gate that records nothing when it could not run reads as one that passed."""
    monkeypatch.delenv("EXAMLOPS_DATA_CONTRACT_GATE", raising=False)
    for report in (
        gate.data_contract_gate("NoContractAtAll", None),
        gate.data_contract_gate("NoContractAtAll", None, is_dummy=True),
    ):
        assert report["validated"] is False
        assert report["reason"]


# ── the inference gate ────────────────────────────────────────────────────────


@pytest.fixture
def ingress(monkeypatch):
    """The ingress module, imported by its **fully-qualified package path**.

    Not ``import app``: ``serving/inference_pipeline/app.py`` is one of several modules in this
    repo called ``app`` (the dashboard backend has one too), so the bare name resolves to
    whichever landed in ``sys.modules`` first — this test passed alone and failed under the
    parallel suite, which is the run that counts. Nor by loading the file fresh: executing it a
    second time re-runs its ``@serve.deployment`` decorators, which try to pickle a thread lock.
    The qualified import is what the other inference-pipeline tests already use.
    """
    import serving.inference_pipeline.app as module

    monkeypatch.delenv("EXAMLOPS_INFERENCE_EMBEDDING_DIM", raising=False)
    return module


def test_a_missing_required_field_is_a_422_not_a_crash(ingress):
    ok, errors = ingress._validate_payload({"embedding": [0.1]})
    assert ok is False
    assert "num_nodes" in errors[0]


def test_a_complete_payload_passes(ingress):
    assert ingress._validate_payload({"embedding": [0.1], "num_nodes": 4})[0] is True


def test_a_wrong_width_embedding_is_rejected_when_a_dim_is_declared(ingress, monkeypatch):
    """The check the hand-rolled presence test structurally could not make."""
    monkeypatch.setenv("EXAMLOPS_INFERENCE_EMBEDDING_DIM", "384")

    ok, errors = ingress._validate_payload({"embedding": [0.1, 0.2], "num_nodes": 4})

    assert ok is False
    assert "384" in errors[0]


def test_the_right_width_passes(ingress, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_INFERENCE_EMBEDDING_DIM", "3")
    assert ingress._validate_payload({"embedding": [1, 2, 3], "num_nodes": 4})[0] is True


def test_no_declared_dim_means_no_width_check(ingress):
    """384 is a fact about a use case; the platform must not default to it."""
    assert ingress._validate_payload({"embedding": [0.1], "num_nodes": 4})[0] is True
    assert ingress._embedding_dim() is None


def test_a_non_integer_dim_is_ignored_not_crashed_on(ingress, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_INFERENCE_EMBEDDING_DIM", "wide")
    assert ingress._embedding_dim() is None
    assert ingress._validate_payload({"embedding": [0.1], "num_nodes": 4})[0] is True


def test_validation_never_raises_on_a_malformed_payload(ingress, monkeypatch):
    """Serving must answer 4xx, never 5xx, on a bad request (R9)."""
    monkeypatch.setenv("EXAMLOPS_INFERENCE_EMBEDDING_DIM", "384")
    ok, errors = ingress._validate_payload({"embedding": 7, "num_nodes": 4})
    assert ok is False and errors


def test_the_ingress_degrades_to_a_presence_check_without_the_contract_package(
    ingress, monkeypatch
):
    """A replica that cannot import `pipelines` must still serve; refusing every request
    because a *validator* is missing is a worse failure than the one the gate prevents."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _no_contracts(name, *args, **kwargs):
        if name == "pipelines.contracts":
            raise ImportError("not installed in this replica")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _no_contracts)

    assert ingress._validate_payload({"embedding": [0.1], "num_nodes": 4})[0] is True
    assert ingress._validate_payload({"embedding": [0.1]})[0] is False


def test_the_ingress_and_the_contract_layer_share_one_validator():
    """Two validators, one of which knew about embedding width and was never asked."""
    src = (ROOT / "serving" / "inference_pipeline" / "app.py").read_text()
    assert "validate_request" in src
    assert 'for field in ("embedding", "num_nodes")' not in src


def test_the_contract_layer_still_exposes_the_shape_the_ingress_relies_on():
    _contract("ShapeDS", embedding_dim("embedding", 3))
    from pipelines.contracts import load_contract

    assert load_contract("ShapeDS") is not None
