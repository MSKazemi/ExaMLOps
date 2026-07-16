# tests/unit/test_data_contracts.py
"""A5 — Data contracts & quality gates (ADR 0005, spec A5).

GWT-1 schema fail-closed · GWT-2 embedding dim · GWT-3 warn severity ·
GWT-4 request validation (serving gate) · GWT-5 CLI non-zero exit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
sys.path.insert(0, str(REPO_ROOT / "platform" / "cli" / "src"))

from examlops.platform_db import (  # noqa: E402
    get_data_quality_checks,
    init_db,
    record_data_quality_check,
)
from pipelines.contracts import (  # noqa: E402
    validate_request,
)
from pipelines.contracts.fdata import CONTRACT  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()


def _good_df(n=5):
    return pd.DataFrame(
        {
            "pclass": ["memory-bound", "compute-bound"] * n,
            "mbwidth": [10.0, 20.0] * n,
            "embedding": [[0.1] * 384 for _ in range(2 * n)],
        }
    )


def test_valid_dataframe_passes():
    result = CONTRACT.validate(_good_df())
    assert result.passed is True
    assert result.score == 1.0
    assert not result.errors


def test_gwt1_missing_column_fails_closed():
    df = _good_df().rename(columns={"pclass": "renamed"})
    result = CONTRACT.validate(df)
    assert result.passed is False
    names = [e["name"] for e in result.errors]
    assert any("pclass" in n for n in names)


def test_gwt2_wrong_embedding_dim_fails():
    df = _good_df()
    df["embedding"] = [[0.1] * 128 for _ in range(len(df))]
    result = CONTRACT.validate(df)
    assert result.passed is False
    assert any("embedding_dim" in e["name"] for e in result.errors)


def test_categorical_domain_violation_fails():
    df = _good_df()
    df.loc[0, "pclass"] = "io-bound"
    result = CONTRACT.validate(df)
    assert result.passed is False


def test_range_violation_fails():
    df = _good_df()
    df.loc[0, "mbwidth"] = -1.0
    result = CONTRACT.validate(df)
    assert result.passed is False


def test_gwt3_warn_severity_does_not_block():
    from pipelines.contracts import DataContract, not_null

    df = pd.DataFrame({"x": [1.0, None, None, None]})  # 75% null
    contract = DataContract(
        dataset="X", version="1", checks=[not_null("x", max_null_rate=0.1, severity="warn")]
    )
    result = contract.validate(df)
    assert result.passed is True  # warn does not fail the gate
    assert len(result.warnings) == 1


# --- GWT-4: request/inference gate ------------------------------------------


def test_gwt4_valid_request_ok():
    ok, errors = validate_request(
        {"embedding": [0.1] * 384, "num_nodes": 4},
        required=["embedding", "num_nodes"],
        embedding_field="embedding",
        embedding_dim=384,
    )
    assert ok is True
    assert errors == []


def test_gwt4_bad_request_flagged_not_crash():
    ok, errors = validate_request(
        {"embedding": [0.1] * 128},  # wrong dim + missing num_nodes
        required=["embedding", "num_nodes"],
        embedding_field="embedding",
        embedding_dim=384,
    )
    assert ok is False
    assert any("num_nodes" in e for e in errors)
    assert any("384" in e for e in errors)


def test_request_range_violation():
    ok, errors = validate_request(
        {"num_nodes": -1},
        required=["num_nodes"],
        ranges={"num_nodes": (0, None)},
    )
    assert ok is False


def test_request_non_sequence_embedding_no_crash():
    ok, errors = validate_request(
        {"embedding": 5},
        required=["embedding"],
        embedding_field="embedding",
        embedding_dim=384,
    )
    assert ok is False
    assert any("not a sequence" in e for e in errors)


# --- persistence -------------------------------------------------------------


def test_record_and_get_quality_check():
    result = CONTRACT.validate(_good_df())
    record_data_quality_check("FData", result, revision="rev1", stage="validate", actor="me")
    rows = get_data_quality_checks("FData")
    assert len(rows) == 1
    assert rows[0]["status"] == "PASS"
    assert rows[0]["revision"] == "rev1"
    assert rows[0]["stage"] == "validate"
    assert rows[0]["score"] == 1.0


def test_record_failed_check():
    df = _good_df().rename(columns={"pclass": "x"})
    result = CONTRACT.validate(df)
    record_data_quality_check("FData", result, stage="train", actor="me")
    assert get_data_quality_checks("FData")[0]["status"] == "FAIL"
