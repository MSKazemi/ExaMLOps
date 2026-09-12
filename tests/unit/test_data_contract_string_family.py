# tests/unit/test_data_contract_string_family.py
"""A column of strings, on pandas 2 and pandas 3 (BL-067).

The FData contract declared `pclass` as `dtype="object"`, matched as a substring of the dtype's
name. Under pandas 3 a parquet string column reads back as `str` (verified on 3.0.5), so the check
failed — an error-severity check, so the training gate refused every FData run the day pandas was
upgraded. pandas is held below 3 only because the `synth` extra (SDV) caps it; the upgrade will
come. `dtype="string"` names the *family*: `object` holding `str`, `string`, or `str`.

These build pandas 3's `str` dtype on pandas 2.3 with its `future.infer_string` option, the switch
pandas added so code could be tested against the new default.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipelines import contracts as c  # noqa: E402
from pipelines.contracts.fdata import CONTRACT as FDATA  # noqa: E402

ENGINES = ("python", "auto") if importlib.util.find_spec("pandera") else ("python",)


def _pandas3_strings(values):
    """A string column as pandas 3 reads it from parquet: dtype `str`."""
    with pd.option_context("future.infer_string", True):
        col = pd.Series(values)
    assert str(col.dtype) == "str"
    return col


def _fdata(pclass):
    n = len(pclass)
    return pd.DataFrame(
        {"pclass": pclass, "mbwidth": [10.0] * n, "embedding": [[0.1] * 384 for _ in range(n)]}
    )


def _passed(contract, df, engine, monkeypatch, name):
    monkeypatch.setenv("EXAMLOPS_CONTRACT_ENGINE", engine)
    result = contract.validate(df)
    return next(r for r in result.checks if r["name"] == name)


# ── the FData contract, as each pandas version reads the data ────────────────


@pytest.mark.parametrize(
    "spelling",
    ["object (pandas 2)", "str (pandas 3)", "string"],
)
def test_fdata_accepts_its_strings_however_pandas_spells_them(spelling, monkeypatch):
    values = ["memory-bound", "compute-bound"] * 3
    pclass = {
        "object (pandas 2)": pd.Series(values, dtype=object),
        "str (pandas 3)": _pandas3_strings(values),
        "string": pd.Series(values, dtype="string"),
    }[spelling]

    for engine in ENGINES:
        monkeypatch.setenv("EXAMLOPS_CONTRACT_ENGINE", engine)
        result = FDATA.validate(_fdata(pclass))
        assert result.passed, (spelling, engine, result.errors)


def test_the_v1_spelling_is_what_broke():
    """The regression, pinned: `dtype="object"` against pandas 3's `str` fails."""
    df = pd.DataFrame({"pclass": _pandas3_strings(["memory-bound"])})

    result = c.DataContract("FData", "1", [c.column_present("pclass", dtype="object")]).validate(df)

    assert result.passed is False
    assert FDATA.version == "2", "a changed contract gets a new version"


# ── what the family accepts ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "col,ok",
    [
        (pd.Series(["a", None, "b"], dtype=object), True),  # nulls are not values
        (pd.Series([None, None], dtype=object), True),
        (pd.Series([], dtype=object), True),
        (pd.Series(["a", pd.NA], dtype="string"), True),
        (pd.Series(["a", np.nan], dtype=pd.StringDtype(na_value=np.nan)), True),  # == "str"
        (pd.Series(["a", 1], dtype=object), False),  # a number hiding in an object column
        (pd.Series([["a"], ["b"]], dtype=object), False),  # lists, like an embedding column
        (pd.Series(["a", "b"], dtype="category"), False),  # a categorical is its own type
        (pd.Series([1.0, 2.0]), False),
    ],
    ids=["nulls", "all-null", "empty", "string", "str", "mixed", "lists", "category", "float"],
)
def test_the_string_family(col, ok, monkeypatch):
    contract = c.DataContract("t", "1", [c.column_present("s", dtype=c.STRING)])

    for engine in ENGINES:
        check = _passed(contract, pd.DataFrame({"s": col}), engine, monkeypatch, "column:s")
        assert check["passed"] is ok, (engine, check)


def test_a_failure_says_what_is_wrong():
    mixed = pd.DataFrame({"s": pd.Series(["a", 3], dtype=object)})
    floats = pd.DataFrame({"s": [1.5]})
    contract = c.DataContract("t", "1", [c.column_present("s", dtype=c.STRING)])

    assert contract.validate(mixed).checks[0]["observed"] == "s holds a non-string value (int)"
    assert (
        contract.validate(floats).checks[0]["observed"] == "s dtype float64 is not a string dtype"
    )


def test_other_dtypes_still_match_as_substrings():
    df = pd.DataFrame({"x": np.array([1.0], dtype=np.float32)})

    assert c.DataContract("t", "1", [c.column_present("x", dtype="float")]).validate(df).passed
