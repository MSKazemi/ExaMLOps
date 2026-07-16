"""A8 — signed reproducibility bundles (ADR 0038)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "test-signing-key")
    from examlops import platform_db

    platform_db.init_db()
    yield


def test_gwt1_bundle_captures_and_signs():
    from examlops.reproducibility import build_bundle

    b = build_bundle(
        "JPCP",
        "17",
        dataset_name="PM100",
        dataset_revision="rev-1",
        hyperparams={"lr": 0.01},
        seeds={"global": 42},
        metrics={"rmse": 5.0},
    )
    assert b.signed is True
    assert b.manifest["hyperparams"] == {"lr": 0.01}
    assert b.manifest["seeds"] == {"global": 42}
    assert "nondeterminism_caveat" in b.manifest
    # code + env inputs captured (running inside a git repo with a lockfile).
    kinds = {i["kind"] for i in b.manifest["inputs"]}
    assert "dataset" in kinds


def test_unsigned_when_no_key(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY", raising=False)
    # Make the secrets fallback fail too so no key is resolvable.
    import examlops.supplychain as sc

    monkeypatch.setattr(sc, "_signing_key", lambda: (_ for _ in ()).throw(RuntimeError("no key")))
    from examlops.reproducibility import build_bundle

    b = build_bundle("JPCP", "18")
    assert b.signed is False
    assert b.signature is None


def test_gwt3_reproduce_never_claims_bit_exact():
    from examlops.reproducibility import build_bundle, reproduce

    build_bundle("JPCP", "17", metrics={"rmse": 5.0})
    r = reproduce("JPCP", "17", observed_metrics={"rmse": 5.1})
    assert r.bit_exact is False
    assert any("bit-exact" in d for d in r.details)


def test_gwt2_reproduce_metric_match_within_tolerance():
    from examlops.reproducibility import build_bundle, reproduce

    build_bundle("JPCP", "17", metrics={"rmse": 5.0})  # default rel tol 5%
    within = reproduce("JPCP", "17", observed_metrics={"rmse": 5.2})  # 4% off
    assert within.metric_match is True
    outside = reproduce("JPCP", "17", observed_metrics={"rmse": 6.0})  # 20% off
    assert outside.metric_match is False


def test_reproduce_without_observed_is_plan_only():
    from examlops.reproducibility import build_bundle, reproduce

    build_bundle("JPCP", "17", metrics={"rmse": 5.0})
    r = reproduce("JPCP", "17")
    assert r.metric_match is None
    assert r.details  # plan steps present


def _rev(dataset, revision_id, backend="minio"):
    from types import SimpleNamespace

    return SimpleNamespace(
        backend=backend,
        dataset=dataset,
        revision_id=revision_id,
        kind="content",
        uri=f"s3://{dataset}/{revision_id}",
        schema_hash="abc",
    )


def test_gwt4_verify_flags_purged_dataset():
    from examlops import platform_db
    from examlops.reproducibility import build_bundle, verify_bundle

    # Record a dataset revision, build a bundle referencing it → reproducible.
    platform_db.record_dataset_revision(_rev("PM100", "rev-1"))
    build_bundle("JPCP", "17", dataset_name="PM100", dataset_revision="rev-1")
    ok = verify_bundle("JPCP", "17")
    assert ok.reproducible is True

    # A bundle referencing a NON-recorded revision → non-reproducible (purged).
    build_bundle("JPCP", "18", dataset_name="PM100", dataset_revision="rev-GONE")
    rot = verify_bundle("JPCP", "18")
    assert rot.reproducible is False
    assert any("dataset" in p for p in rot.problems)


def test_verify_no_bundle():
    from examlops.reproducibility import verify_bundle

    r = verify_bundle("NOPE", "1")
    assert r.reproducible is False
    assert "no bundle found" in r.problems


def test_gwt5_technical_evidence_shape():
    from examlops.reproducibility import build_bundle, technical_evidence

    build_bundle("JPCP", "17", dataset_name="PM100", dataset_revision="rev-1")
    ev = technical_evidence("JPCP", "17")
    assert ev["present"] is True
    assert ev["signed"] is True
    assert "dataset" in ev["captured_inputs"]


def test_bundle_versioning_increments():
    from examlops.reproducibility import build_bundle

    b1 = build_bundle("JPCP", "17", metrics={"rmse": 5.0})
    b2 = build_bundle("JPCP", "17", metrics={"rmse": 4.9})
    assert b2.bundle_version == b1.bundle_version + 1


def test_build_audited():
    from examlops import platform_db
    from examlops.reproducibility import build_bundle

    build_bundle("JPCP", "17")
    with platform_db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action='repro_bundle_built'"
        ).fetchall()
    assert len(rows) == 1


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r = runner.invoke(app, ["reproduce", "build", "JPCP", "17", "--metrics", '{"rmse": 5.0}'])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["reproduce", "run", "JPCP", "17", "--observed", '{"rmse": 5.1}'])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["reproduce", "list"])
    assert r.exit_code == 0, r.output
    assert "JPCP" in r.output
