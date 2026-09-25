"""ADR 0027 — the unbuilt clauses of the NIST AI RMF backbone.

* decision 3: D6 (authorization) and D7 (secrets) evidence, so the RBAC control rests on
  authorization records instead of audit-trail coverage;
* decision 2: features declare the controls they serve and the evidence they emit, validated in
  both directions;
* the catalogue encodes the framework's 72 subcategories, and the report rolls platform controls
  up to them — a subcategory no control reaches is *organisational*, never covered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in (
        "EXAMLOPS_MULTITENANCY",
        "EXAMLOPS_OPENFGA_URL",
        "EXAMLOPS_OPENFGA_STORE_ID",
        "EXAMLOPS_VAULT_ADDR",
        "EXAMLOPS_IAM_CONFIG",
        "EXAMLOPS_GOVERNANCE_SECRET_MAX_AGE_DAYS",
        "EXAMLOPS_SECRETS_KEYS",
        "EXAMLOPS_SECRETS_ACTIVE_KEY",
        "DASHBOARD_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    from cryptography.fernet import Fernet

    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())
    from examlops import platform_db

    platform_db.init_db()
    yield


def _by_id(report):
    return {c.control.id: c for c in report.controls}


def _assign(project: str, model: str) -> None:
    from examlops.data import get_db

    with get_db() as conn:
        conn.execute("INSERT INTO project_models (project, model) VALUES (?, ?)", (project, model))


# ── the framework layer ─────────────────────────────────────────────────────────────────────


def test_shipped_catalogue_features_and_framework_validate_clean():
    from examlops.governance import validate_mapping

    errors = validate_mapping()
    assert errors == [], [f"{e.control_id}: {e.problem}" for e in errors]


def test_catalogue_encodes_all_72_ai_rmf_subcategories():
    from examlops.governance import NIST_SUBCATEGORY_COUNTS, load_subcategories

    subs = load_subcategories()
    assert len(subs) == 72 == sum(NIST_SUBCATEGORY_COUNTS.values())
    assert len({s.id for s in subs}) == 72
    for fn, n in NIST_SUBCATEGORY_COUNTS.items():
        assert sum(1 for s in subs if s.function == fn) == n
    assert all(s.title for s in subs)


def test_a_truncated_framework_fails_validation(monkeypatch):
    import examlops.governance as gov

    real = gov.load_subcategories()
    monkeypatch.setattr(gov, "load_subcategories", lambda: real[:-1])  # drop MANAGE-4.3
    problems = [e.problem for e in gov.validate_mapping()]
    assert any("Manage: 12 subcategories encoded, AI RMF 1.0 defines 13" in p for p in problems)


def test_a_control_pointing_at_an_unknown_subcategory_fails(monkeypatch):
    import examlops.governance as gov

    real = gov.load_catalogue()
    real[0].nist_subcategories = ["GOVERN-9.9"]
    monkeypatch.setattr(gov, "load_catalogue", lambda: real)
    assert any("GOVERN-9.9" in e.problem for e in gov.validate_mapping())


def test_retired_rbac_id_resolves_to_its_reanchored_control():
    from examlops.governance import resolve_control

    c = resolve_control("GOVERN-4.1")
    assert c is not None and c.id == "GOVERN-2.1"
    assert set(c.evidence) == {"access_documented", "access_enforced"}
    assert "record_keeping" not in c.evidence
    assert resolve_control("NOPE-1.1") is None


# ── decision 2: feature declarations ───────────────────────────────────────────────────────


def _features_with(monkeypatch, mutate):
    import examlops.governance as gov

    feats = gov.load_features()
    mutate(feats)
    monkeypatch.setattr(gov, "load_features", lambda: feats)
    return [f"{e.control_id}: {e.problem}" for e in gov.validate_mapping()]


def test_every_control_is_declared_by_a_feature_and_every_collector_owned():
    from examlops.compliance import _COLLECTORS
    from examlops.governance import load_catalogue, load_features

    feats = load_features()
    for c in load_catalogue():
        for ev in c.evidence:
            assert any(c.id in f.controls and ev in f.emits for f in feats), (c.id, ev)
    assert set(_COLLECTORS) <= {ev for f in feats for ev in f.emits}


def test_feature_claiming_a_control_it_emits_nothing_for_is_rejected(monkeypatch):
    def mutate(feats):
        next(f for f in feats if f.id == "C8").controls.append("GOVERN-2.1")

    problems = _features_with(monkeypatch, mutate)
    assert any("feature C8: claims GOVERN-2.1 but emits none" in p for p in problems)


def test_evidence_nobody_declares_is_rejected(monkeypatch):
    def mutate(feats):
        feats[:] = [f for f in feats if f.id != "D7"]

    problems = _features_with(monkeypatch, mutate)
    assert any(
        "MEASURE-2.7: evidence 'secrets_managed' is emitted by no feature" in p for p in problems
    )
    assert any("collector 'secrets_rotation' is emitted by no feature" in p for p in problems)


def test_unresolvable_module_unknown_control_and_bad_evidence_are_rejected(monkeypatch):
    def mutate(feats):
        feats[0].module = "examlops.does_not_exist_anywhere"
        feats[0].controls.append("MAP-9.9")
        feats[0].emits.append("no_such_collector")

    problems = _features_with(monkeypatch, mutate)
    assert any("does not resolve" in p for p in problems)
    assert any("unknown control 'MAP-9.9'" in p for p in problems)
    assert any("'no_such_collector', which has no collector" in p for p in problems)


def test_duplicate_feature_id_is_rejected(monkeypatch):
    problems = _features_with(monkeypatch, lambda feats: feats.append(feats[0]))
    assert any("duplicate feature id" in p for p in problems)


# ── decision 3: D6 authorization evidence ──────────────────────────────────────────────────


def test_audit_trail_alone_no_longer_satisfies_the_rbac_control():
    """The ADR's named gap: audit coverage used to satisfy GOVERN-4.1 with no authz evidence."""
    from examlops import platform_db
    from examlops.compliance import classify_system
    from examlops.governance import governance_report

    classify_system("JPCP", "high", "HPC triage", "internal", "tester")
    platform_db.write_audit_event("cli", "tester", "promotion", "JPCP", {"to": "Production"})
    rep = _by_id(governance_report("default", model="JPCP"))
    assert rep["GOVERN-1.1"].status == "satisfied"  # the audit trail is real evidence …
    assert rep["GOVERN-2.1"].status == "gap"  # … of logging, not of access control
    assert rep["GOVERN-2.1"].features == ["D6"]


def test_viewer_grants_without_an_owner_do_not_document_accountability():
    from examlops.authz import grant
    from examlops.compliance.security_evidence import ev_access_documented

    grant("alice", "viewer", "model:JPCP", actor="t")
    present, content = ev_access_documented("JPCP", "default")
    assert present is False
    assert "no owner" in content


def test_owner_via_project_membership_documents_roles_but_not_enforcement(monkeypatch):
    from examlops.authz import grant
    from examlops.compliance import classify_system
    from examlops.governance import governance_report

    classify_system("JPCP", "high", "p", "c", "tester")
    _assign("acme", "JPCP")
    grant("alice", "owner", "project:acme", actor="t")
    rep = _by_id(governance_report("default", model="JPCP"))
    rbac = rep["GOVERN-2.1"]
    assert rbac.present_evidence == ["access_documented"]
    assert rbac.missing_evidence == ["access_enforced"]  # single-tenant mode allows everything
    assert rbac.status == "partial"


def test_rbac_satisfied_only_when_documented_and_enforced(monkeypatch):
    from examlops.authz import check, grant
    from examlops.compliance import classify_system
    from examlops.compliance.security_evidence import ev_access_enforced
    from examlops.governance import governance_report

    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    classify_system("JPCP", "high", "p", "c", "tester")
    grant("alice", "owner", "project:acme/model:JPCP", actor="t")
    assert check("mallory", "viewer", "project:acme/model:JPCP") is False  # a recorded deny

    present, content = ev_access_enforced("JPCP", "default")
    assert present is True
    assert "1 authz_deny" in content and "local relation store" in content

    rep = governance_report("default", model="JPCP")
    rbac = _by_id(rep)["GOVERN-2.1"]
    assert rbac.status == "satisfied"
    # Enforcement is read from the environment, so it is never passed off as verified.
    assert any("reporting process's environment" in n for n in rbac.evidence_notes)


def test_relations_on_another_model_are_not_evidence_for_this_one():
    from examlops.authz import grant
    from examlops.compliance.security_evidence import ev_access_documented

    grant("alice", "owner", "model:OTHER", actor="t")
    grant("alice", "owner", "project:x/model:JPCPX", actor="t")  # a prefix, not the model
    assert ev_access_documented("JPCP", "default")[0] is False


# ── decision 3: D7 secrets evidence ────────────────────────────────────────────────────────


def test_no_managed_secrets_is_a_gap():
    from examlops.compliance.security_evidence import ev_secrets_managed, ev_secrets_rotation

    assert ev_secrets_managed("JPCP", "default")[0] is False
    present, content = ev_secrets_rotation("JPCP", "default")
    assert present is False and "nothing to rotate" in content


def test_fresh_encrypted_secrets_satisfy_managed_and_rotation():
    from examlops.compliance.security_evidence import ev_secrets_managed, ev_secrets_rotation
    from examlops.secrets import rotate_secret, set_secret

    set_secret("db/password", "s3cret", tenant="default", actor="t")
    rotate_secret("db/password", tenant="default", actor="t")
    present, content = ev_secrets_managed("JPCP", "default")
    assert present and "1 secret(s) Fernet-encrypted" in content
    present, content = ev_secrets_rotation("JPCP", "default")
    assert present and "1 audited rotation" in content


def test_a_secret_older_than_the_window_fails_rotation():
    from examlops.compliance.security_evidence import ev_secrets_rotation
    from examlops.data import get_db
    from examlops.secrets import set_secret

    set_secret("db/password", "s3cret", tenant="default", actor="t")
    set_secret("api/token", "t0ken", tenant="default", actor="t")
    with get_db() as conn:
        conn.execute(
            "UPDATE secrets_store SET updated_at='2020-01-01 00:00:00' WHERE path='api/token'"
        )
    present, content = ev_secrets_rotation("JPCP", "default")
    assert present is False
    assert "1 of 2 secret(s)" in content and "90 day(s)" in content


def test_a_kek_rewrap_is_not_a_credential_rotation(monkeypatch):
    """`exa secrets rewrap` re-encrypts the SAME value under a new KEK. It must not reset the
    credential's age, or a years-old password would pass the rotation check the moment an operator
    rotated the encryption key."""
    from cryptography.fernet import Fernet

    from examlops.compliance.security_evidence import ev_secrets_rotation
    from examlops.data import get_db
    from examlops.secrets import get_secret, rewrap_secrets, set_secret

    set_secret("db/password", "s3cret", tenant="default", actor="alice")
    with get_db() as conn:
        conn.execute(
            "UPDATE secrets_store SET updated_at='2020-01-01 00:00:00' WHERE path='db/password'"
        )
    assert ev_secrets_rotation("JPCP", "default")[0] is False

    old = Fernet.generate_key().decode()
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEYS", f"k2:{old}")
    monkeypatch.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "k2")
    summary = rewrap_secrets(actor="ops")
    assert summary["rewrapped"] == 1 and summary["failed"] == 0

    with get_db() as conn:
        row = conn.execute(
            "SELECT key_id, version, updated_at, updated_by FROM secrets_store "
            "WHERE path='db/password'"
        ).fetchone()
    assert row["key_id"] == "k2" and row["version"] == 2
    assert str(row["updated_at"]).startswith("2020-01-01") and row["updated_by"] == "alice"
    assert get_secret("db/password", tenant="default") == "s3cret"
    present, content = ev_secrets_rotation("JPCP", "default")
    assert present is False and "1 of 1 secret(s)" in content
    assert "0 audited rotation(s)" in content  # the rewrap event is not counted as one


def test_rotation_window_is_configurable_and_malformed_values_fall_back(monkeypatch):
    from examlops.compliance.security_evidence import (
        DEFAULT_SECRET_MAX_AGE_DAYS,
        secret_max_age_days,
    )

    monkeypatch.setenv("EXAMLOPS_GOVERNANCE_SECRET_MAX_AGE_DAYS", "30")
    assert secret_max_age_days() == 30
    for bad in ("abc", "0", "-5", "999999"):
        monkeypatch.setenv("EXAMLOPS_GOVERNANCE_SECRET_MAX_AGE_DAYS", bad)
        assert secret_max_age_days() == DEFAULT_SECRET_MAX_AGE_DAYS


def test_vault_only_store_is_managed_but_its_rotation_is_not_visible(monkeypatch):
    from examlops.compliance.security_evidence import ev_secrets_managed, ev_secrets_rotation

    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", "http://127.0.0.1:8200")
    assert ev_secrets_managed("JPCP", "default")[0] is True
    present, content = ev_secrets_rotation("JPCP", "default")
    assert present is False and "not visible" in content


def test_secrets_evidence_is_tenant_scoped():
    from examlops.compliance.security_evidence import ev_secrets_managed
    from examlops.secrets import set_secret

    set_secret("acme/db", "x", tenant="acme", actor="t")
    assert ev_secrets_managed("JPCP", "acme")[0] is True
    assert ev_secrets_managed("JPCP", "globex")[0] is False


def test_secret_audit_events_carry_their_tenant_on_the_row():
    from examlops.data import get_db
    from examlops.secrets import set_secret

    set_secret("acme/db", "x", tenant="acme", actor="t")
    with get_db() as conn:
        row = conn.execute(
            "SELECT tenant FROM audit_events WHERE action='secret_set' ORDER BY id DESC"
        ).fetchone()
    assert row["tenant"] == "acme"


def test_measure_2_7_needs_integrity_and_secrets():
    from examlops.compliance import classify_system
    from examlops.data import get_db
    from examlops.governance import governance_report
    from examlops.secrets import set_secret

    classify_system("JPCP", "high", "p", "c", "tester")
    with get_db() as conn:
        conn.execute("INSERT INTO model_boms (model, version, bom_json) VALUES ('JPCP', '1', '{}')")
    rep = _by_id(governance_report("default", model="JPCP"))
    assert rep["MEASURE-2.7"].status == "partial"
    assert set(rep["MEASURE-2.7"].missing_evidence) == {"secrets_managed", "secrets_rotation"}

    set_secret("db/password", "s3cret", tenant="default", actor="t")
    rep = _by_id(governance_report("default", model="JPCP"))
    assert rep["MEASURE-2.7"].status == "satisfied"
    assert rep["MEASURE-2.7"].features == ["D3", "D7"]


def test_environmental_impact_from_carbon_records():
    from examlops.compliance.security_evidence import ev_environmental_impact
    from examlops.data import get_db

    assert ev_environmental_impact("JPCP", "default")[0] is False
    with get_db() as conn:
        conn.execute("INSERT INTO carbon_records (model, kwh, co2e_g) VALUES ('jpcp', 1.5, 300.0)")
    present, content = ev_environmental_impact("JPCP", "default")
    assert present and "1.500 kWh" in content and "300.0 gCO2e" in content


def test_a_collector_read_failure_is_a_gap_never_a_pass(monkeypatch):
    from examlops.compliance import security_evidence as sec

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sec.platform_db, "get_db", boom)
    for fn in (
        sec.ev_access_documented,
        sec.ev_secrets_managed,
        sec.ev_secrets_rotation,
        sec.ev_environmental_impact,
    ):
        present, content = fn("JPCP", "default")
        assert present is False and "database is locked" in content


# ── the report: framework roll-up, attribution, audit ──────────────────────────────────────


def test_framework_rollup_counts_organisational_subcategories_honestly():
    from examlops.compliance import classify_system
    from examlops.governance import governance_report, load_catalogue

    classify_system("JPCP", "high", "HPC triage", "internal", "tester")
    rep = governance_report("default", model="JPCP")
    fw = rep.framework_summary
    mapped = {s for c in load_catalogue() for s in c.nist_subcategories}
    assert fw["subcategories"] == 72
    assert fw["platform_evidenced"] == len(mapped)
    assert fw["organisational"] == 72 - len(mapped)
    subs = {s.subcategory.id: s for s in rep.subcategories}
    assert subs["MAP-1.1"].status == "satisfied"  # system_description present
    assert subs["GOVERN-1.6"].status == "satisfied"  # the same inventory evidence
    assert subs["MEASURE-2.4"].controls == ["MANAGE-2.2"]  # reached via nist_subcategories
    assert subs["GOVERN-2.2"].status == "organisational"  # training: not platform evidence
    assert sum(fw["by_function"]["Govern"].values()) == 19


def test_subcategory_is_satisfied_only_when_all_its_controls_are():
    from examlops.governance import (
        Control,
        ControlCoverage,
        _subcategory_coverage,
    )

    a = ControlCoverage(Control("A", "Map", "", ["x"], nist_subcategories=["MAP-1.1"]), "satisfied")
    b = ControlCoverage(Control("B", "Map", "", ["x"], nist_subcategories=["MAP-1.1"]), "gap")
    out = {s.subcategory.id: s.status for s in _subcategory_coverage([a, b])}
    assert out["MAP-1.1"] == "partial"
    out = {s.subcategory.id: s.status for s in _subcategory_coverage([b])}
    assert out["MAP-1.1"] == "gap"


def test_persisted_report_is_audited_under_its_tenant_with_framework_figures():
    from examlops.data import get_db
    from examlops.governance import governance_report

    governance_report("acme", persist_by="auditor")
    with get_db() as conn:
        row = conn.execute(
            "SELECT tenant, details FROM audit_events WHERE action='governance_report'"
        ).fetchone()
    assert row["tenant"] == "acme"
    details = json.loads(row["details"])
    assert details["catalogue_version"] == "2.1.0"
    assert details["framework"]["subcategories"] == 72


def test_cli_features_subcategories_and_report_json():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r = runner.invoke(app, ["--json", "governance", "features"])
    assert r.exit_code == 0, r.output
    feats = json.loads(r.output)
    assert {"D6", "D7"} <= {f["id"] for f in feats}

    r = runner.invoke(app, ["--json", "governance", "catalogue", "--subcategories"])
    assert r.exit_code == 0, r.output
    assert len(json.loads(r.output)["subcategories"]) == 72

    r = runner.invoke(app, ["--json", "governance", "report"])
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["framework"]["subcategories"] == 72
    assert any(c["id"] == "GOVERN-2.1" and c["aliases"] == ["GOVERN-4.1"] for c in body["controls"])

    r = runner.invoke(app, ["governance", "report"])
    assert r.exit_code == 0, r.output
    assert "AI RMF 1.0 framework" in r.output
