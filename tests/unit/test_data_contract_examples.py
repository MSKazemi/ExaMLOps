# tests/unit/test_data_contract_examples.py
"""ADR 0005 clause 4 — the contract gate, exercised in CI (BL-066).

The clause asks for "`exa data validate` + a blocking CI step". A site's CI validates its own data;
*this* repository's CI has no data, and a step that validates nothing would be a gate in name only.
What it can prove — and what was missing — is that each contract still accepts the data it exists to
describe and still rejects what it must. Nothing ran a contract against good data, which is why the
`pclass` dtype check could have refused every FData row on pandas 3 (BL-067) with a green suite.

Each contract therefore declares `EXAMPLES` beside it (`ContractExamples`), and these run them:
through the contract directly on both engines, and end to end through the real `exa data validate`
on Parquet, whose exit code is the gate a CI step would block on. This test runs in the unit job,
which is the blocking check on `main`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "pipelines" / "contracts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "platform" / "cli" / "src"))

from pipelines.contracts import load_contract, load_examples  # noqa: E402

ENGINES = ("python", "auto") if importlib.util.find_spec("pandera") else ("python",)

#: Every dataset with a contract module. `_pandera` is the engine, not a dataset.
DATASETS = sorted(
    p.stem for p in CONTRACTS_DIR.glob("*.py") if not p.stem.startswith(("_", "test_"))
)


def test_there_are_contracts_to_check():
    assert DATASETS, "no contract modules found — this guard would pass vacuously"


@pytest.mark.parametrize("dataset", DATASETS)
def test_every_contract_ships_examples(dataset):
    """A contract with no examples is a contract nothing ever ran."""
    assert load_examples(dataset) is not None, (
        f"pipelines/contracts/{dataset}.py declares no EXAMPLES — add ContractExamples("
        "valid=[…], invalid=[(rows, 'check:name'), …])"
    )


@pytest.mark.parametrize("dataset", DATASETS)
@pytest.mark.parametrize("engine", ENGINES)
def test_a_contract_accepts_the_data_it_describes(dataset, engine, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONTRACT_ENGINE", engine)
    contract, examples = load_contract(dataset), load_examples(dataset)

    result = contract.validate(pd.DataFrame(examples.valid))

    assert result.passed, [(c["name"], c["observed"]) for c in result.errors]
    assert result.score == 1.0, [
        (c["name"], c["observed"]) for c in result.checks if not c["passed"]
    ]


@pytest.mark.parametrize("dataset", DATASETS)
@pytest.mark.parametrize("engine", ENGINES)
def test_a_contract_rejects_what_it_must(dataset, engine, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONTRACT_ENGINE", engine)
    contract, examples = load_contract(dataset), load_examples(dataset)

    for rows, expected in examples.invalid:
        result = contract.validate(pd.DataFrame(rows))
        failed = {c["name"] for c in result.errors}
        assert not result.passed, f"{dataset}: {expected} example passed"
        assert expected in failed, f"{dataset}: expected {expected} to fail, got {sorted(failed)}"


# ── the gate a CI step blocks on: the real CLI, on real Parquet ──────────────


def _validate(dataset: str, rows: list[dict], tmp_path: Path, db: Path):
    """Run `exa data validate` exactly as a CI step would. Returns (exit code, stdout)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_parquet(data_dir / "part-0.parquet")
    env = {
        **os.environ,
        "PLATFORM_DB": str(db),
        "EXAMLOPS_CONFIG": str(tmp_path / "no-config.toml"),
        "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{REPO_ROOT / 'platform' / 'cli' / 'src'}",
    }
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "examlops.cli",
            "--json",
            "data",
            "validate",
            dataset,
            "--path",
            str(data_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return run.returncode, run.stdout


def _parquet_writable(invalid):
    """The first rejection example Parquet can hold.

    Not every one can: Parquet gives a column one type, so an example of a *mixed* column (a
    number among the strings) exists only in memory — which a dataplane frame can be. Those are
    exercised against the contract directly, above.
    """
    import pyarrow  # noqa: PLC0415

    for rows, expected in invalid:
        try:
            pyarrow.Table.from_pandas(pd.DataFrame(rows))
        except (pyarrow.lib.ArrowException, TypeError):
            continue
        return rows, expected
    pytest.fail("no rejection example can be written to Parquet")


@pytest.mark.parametrize("dataset", DATASETS)
def test_the_cli_exits_zero_on_data_the_contract_accepts(dataset, tmp_path):
    examples = load_examples(dataset)

    code, out = _validate(dataset, examples.valid, tmp_path, tmp_path / "ok.db")

    assert code == 0, out
    report = json.loads(out)
    assert report["passed"] is True and report["score"] == 1.0


@pytest.mark.parametrize("dataset", DATASETS)
def test_the_cli_exits_non_zero_on_data_the_contract_rejects(dataset, tmp_path):
    """Exit 1 is what a CI step — or a promotion script — blocks on."""
    rows, expected = _parquet_writable(load_examples(dataset).invalid)

    code, out = _validate(dataset, rows, tmp_path, tmp_path / "bad.db")

    assert code == 1, out
    report = json.loads(out)
    assert report["passed"] is False
    assert expected in {c["name"] for c in report["checks"] if not c["passed"]}


@pytest.mark.parametrize("dataset", DATASETS)
def test_each_run_persists_its_score(dataset, tmp_path):
    """Clause 4's other half: the result and its quality score land in `data_quality_checks`."""
    from examlops.platform_db import get_data_quality_checks

    db = tmp_path / "persist.db"
    _validate(dataset, load_examples(dataset).valid, tmp_path, db)

    os.environ["PLATFORM_DB"] = str(db)
    try:
        (row,) = get_data_quality_checks(dataset)
    finally:
        del os.environ["PLATFORM_DB"]
    assert (row["status"], row["score"], row["stage"]) == ("PASS", 1.0, "validate")
    assert row["engine"] in ("pandera", "python")
