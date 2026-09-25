"""ADR 0029 decision 2 (D6) — the data-residency policy and its ``residency`` gate.

Datasheet ``distribution.residency`` × ``clusters.yaml`` ``region``, applied at the real training
entry point (``exa pipeline run --cluster``'s cluster resolution) — including ``--cluster auto``,
where a non-compliant cluster must never be a placement candidate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import typer
import yaml

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.policy_engine import residency as rz  # noqa: E402

_HPC_ENV = (
    "EXAMLOPS_HPC_SCHEDULER",
    "EXAMLOPS_HPC_TRANSPORT",
    "EXAMLOPS_HPC_SSH_HOST",
    "EXAMLOPS_HPC_SSH_USER",
    "EXAMLOPS_HPC_SSH_KEY",
    "EXAMLOPS_HPC_SSH_PORT",
)


@pytest.fixture
def site(tmp_path, monkeypatch):
    sheets = tmp_path / "sheets"
    sheets.mkdir()
    monkeypatch.setenv("EXAMLOPS_DATASHEETS_DIR", str(sheets))
    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.setattr("examlops.policy.POLICY_YAML", tmp_path / "policy.yaml")
    monkeypatch.delenv("EXAMLOPS_POLICY_GATES", raising=False)
    for k in _HPC_ENV:  # resolve_env writes os.environ; register each key for restore
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    from examlops import platform_db

    platform_db.init_db()
    return tmp_path


def _sheet(site: Path, name: str, residency=None) -> None:
    doc: dict = {"distribution": {"license": "x"}}
    if residency is not None:
        doc["distribution"]["residency"] = residency
    (site / "sheets" / f"{name}.yaml").write_text(yaml.safe_dump(doc))


def _cluster(site: Path, name: str, region: str | None) -> None:
    from examlops.data.hpc import set_cluster_state
    from examlops.hpc_registry import register_pending

    register_pending(name, "mock", transport="local")
    path = site / "clusters.yaml"
    doc = yaml.safe_load(path.read_text()) or {}
    clusters = doc.get("clusters", doc)
    if region is not None:
        clusters[name]["region"] = region
    path.write_text(yaml.safe_dump(doc))
    assert set_cluster_state(name, "ACTIVE", approved_by="admin")


# ── the rule ────────────────────────────────────────────────────────────────────────────────
def test_no_datasheet_or_no_declaration_is_unconstrained(site):
    _cluster(site, "hpc-a", None)
    assert rz.residency_reasons(["Absent"], "hpc-a") == []
    _sheet(site, "Free")
    assert rz.allowed_regions("Free") is None
    assert rz.residency_reasons(["Free"], "hpc-a") == []


def test_region_in_list_passes_case_insensitively(site):
    _sheet(site, "EuData", ["EU", "it"])
    _cluster(site, "hpc-eu", "eu")
    assert rz.allowed_regions("EuData") == ["eu", "it"]
    assert rz.residency_reasons(["EuData"], "hpc-eu") == []


def test_region_outside_list_is_refused(site):
    _sheet(site, "EuData", "eu")
    _cluster(site, "hpc-us", "us-east")
    reasons = rz.residency_reasons(["EuData"], "hpc-us")
    assert len(reasons) == 1 and "'us-east'" in reasons[0]


def test_undeclared_cluster_region_fails_closed(site):
    _sheet(site, "EuData", ["eu"])
    _cluster(site, "hpc-x", None)
    assert "declares no region" in rz.residency_reasons(["EuData"], "hpc-x")[0]


def test_unreadable_or_empty_declaration_allows_nowhere(site):
    (site / "sheets" / "Broken.yaml").write_text("distribution: [oops\n")
    _cluster(site, "hpc-eu", "eu")
    assert rz.allowed_regions("Broken") == []
    assert rz.residency_reasons(["Broken"], "hpc-eu")
    _sheet(site, "Empty", [])
    assert rz.residency_reasons(["Empty"], "hpc-eu")


def test_undecodable_datasheet_fails_closed_instead_of_crashing(site):
    (site / "sheets" / "Binary.yaml").write_bytes(b"distribution:\n  residency: \xff\xfe\n")
    _cluster(site, "hpc-eu", "eu")
    assert rz.allowed_regions("Binary") == []
    assert rz.residency_reasons(["Binary"], "hpc-eu")


def test_compliant_clusters_filters_candidates(site):
    _sheet(site, "EuData", ["eu"])
    _cluster(site, "hpc-eu", "eu")
    _cluster(site, "hpc-us", "us")
    names = [
        c["name"]
        for c in rz.compliant_clusters(["EuData"], [{"name": "hpc-eu"}, {"name": "hpc-us"}])
    ]
    assert names == ["hpc-eu"]


# ── the gate at the training entry point ────────────────────────────────────────────────────
def _resolve(cluster: str, dataset: str = "EuData") -> bool:
    from examlops.cli.commands.pipeline import _resolve_cluster_env

    return _resolve_cluster_env(cluster, 0, model="demo", dataset=dataset)


def test_gate_off_leaves_resolution_unchanged(site):
    _sheet(site, "EuData", ["eu"])
    _cluster(site, "hpc-us", "us")
    assert _resolve("hpc-us") is True  # off by default: no residency check at all


def test_gate_enforced_refuses_a_forbidden_region_and_audits(site, monkeypatch):
    import os

    _sheet(site, "EuData", ["eu"])
    _cluster(site, "hpc-us", "us")
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "residency=enforce")
    with pytest.raises(typer.Exit) as exc:
        _resolve("hpc-us")
    assert exc.value.exit_code == 1
    assert "EXAMLOPS_HPC_SCHEDULER" not in os.environ  # never targeted
    from examlops.data.audit import export_audit_events

    assert any(e["action"] == "policy_residency" for e in export_audit_events())


def test_gate_enforced_allows_a_compliant_region(site, monkeypatch):
    import os

    _sheet(site, "EuData", ["eu"])
    _cluster(site, "hpc-eu", "eu")
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "residency=enforce")
    assert _resolve("hpc-eu") is True
    assert os.environ["EXAMLOPS_HPC_SCHEDULER"] == "mock"


def test_auto_placement_never_picks_a_forbidden_region(site, monkeypatch):
    import os

    _sheet(site, "EuData", ["eu"])
    _cluster(site, "a-us", "us")  # sorts first — would win a tie
    _cluster(site, "b-eu", "eu")
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "residency=enforce")
    seen: list[list[str]] = []
    import examlops.hpc_placement as hp

    def spy(ask, clusters, score_fn=None):
        # A placement engine that picks the first candidate it is offered: were the US cluster
        # still a candidate, it would win.
        seen.append([c["name"] for c in clusters])
        first = clusters[0]["name"] if clusters else None
        return hp.PlacementResult(first, f"first of {len(clusters)}")

    monkeypatch.setattr(hp, "choose_cluster", spy)
    assert _resolve("auto") is True
    assert seen == [["b-eu"]]
    assert os.environ["EXAMLOPS_HPC_SCHEDULER"] == "mock"


def test_monitor_mode_does_not_change_auto_placement(site, monkeypatch):
    """`monitor` observes and never blocks: pruning candidates in monitor mode turned a sole
    non-compliant cluster into "no cluster fits" — a block by another name."""
    import os

    _sheet(site, "EuData", ["eu"])
    _cluster(site, "only-us", "us")
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "residency=monitor")
    seen: list[list[str]] = []
    import examlops.hpc_placement as hp

    def spy(ask, clusters, score_fn=None):
        seen.append([c["name"] for c in clusters])
        first = clusters[0]["name"] if clusters else None
        return hp.PlacementResult(first, f"first of {len(clusters)}")

    monkeypatch.setattr(hp, "choose_cluster", spy)
    assert _resolve("auto") is True
    assert seen == [["only-us"]]  # the candidate set is untouched in monitor mode
    assert os.environ["EXAMLOPS_HPC_SCHEDULER"] == "mock"
    from examlops.data.audit import export_audit_events

    assert any(e["action"] == "policy_gate_monitor:residency" for e in export_audit_events())


def test_monitor_mode_records_but_does_not_block(site, monkeypatch):
    _sheet(site, "EuData", ["eu"])
    _cluster(site, "hpc-us", "us")
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "residency=monitor")
    assert _resolve("hpc-us") is True
    from examlops.data.audit import export_audit_events

    assert any(e["action"] == "policy_gate_monitor:residency" for e in export_audit_events())


# ── the datasets a run reads must be named, or the gate refuses (fail closed) ─────────────────
def _pack(site: Path, monkeypatch, models: dict[str, object]) -> None:
    d = site / "models"
    d.mkdir(exist_ok=True)
    for name, doc in models.items():
        path = d / f"{name}.yaml"
        if isinstance(doc, bytes):
            path.write_bytes(doc)
        else:
            path.write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("RAY_MODELS_DIR", str(d))


def _run_resolve(cluster: str, **kw) -> bool:
    from examlops.cli.commands.pipeline import _resolve_cluster_env

    return _resolve_cluster_env(cluster, 0, **kw)


def test_a_run_of_every_model_checks_every_pack_dataset(site, monkeypatch):
    """`exa pipeline run --cluster X` with no --model trains every model: an EU-only dataset of
    any of them must stop the run — it used to resolve to no dataset at all and pass."""
    _pack(site, monkeypatch, {"free": {"datasets": []}, "eu": {"datasets": [{"name": "EuData"}]}})
    _sheet(site, "EuData", ["eu"])
    _cluster(site, "hpc-us", "us")
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "residency=enforce")
    with pytest.raises(typer.Exit):
        _run_resolve("hpc-us")


def test_a_model_run_reads_the_model_yaml(site, monkeypatch):
    _pack(site, monkeypatch, {"demo": {"datasets": [{"name": "EuData"}]}})
    _sheet(site, "EuData", ["eu"])
    _cluster(site, "hpc-us", "us")
    _cluster(site, "hpc-eu", "eu")
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "residency=enforce")
    with pytest.raises(typer.Exit):
        _run_resolve("hpc-us", model="demo")
    assert _run_resolve("hpc-eu", model="demo") is True


@pytest.mark.parametrize("broken", ["missing", "unreadable"])
def test_undeterminable_datasets_are_refused_not_waved_through(site, monkeypatch, broken):
    _pack(site, monkeypatch, {} if broken == "missing" else {"demo": b"datasets: [\xff\n"})
    _cluster(site, "hpc-eu", "eu")
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "residency=enforce")
    with pytest.raises(typer.Exit):
        _run_resolve("hpc-eu", model="demo")


def test_a_custom_registry_needs_an_explicit_dataset(site, monkeypatch):
    _cluster(site, "hpc-eu", "eu")
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "residency=enforce")
    with pytest.raises(typer.Exit):
        _run_resolve("hpc-eu", model="demo", registry="/elsewhere/model_registry.yaml")
    assert _run_resolve("hpc-eu", model="demo", dataset="Free", registry="/x.yaml") is True


def test_gate_off_reads_no_model_yaml(site, monkeypatch):
    """Off by default: resolving a cluster must not start depending on the pack being readable."""
    _pack(site, monkeypatch, {"demo": b"datasets: [\xff\n"})
    _cluster(site, "hpc-us", "us")
    assert _run_resolve("hpc-us", model="demo") is True
