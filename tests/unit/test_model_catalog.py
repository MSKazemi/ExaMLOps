"""The Model Catalog (ADR 0158): publish → browse → pull, and what a pull must NOT do.

The catalog answers "what could I start from?"; the MLflow registry answers "what did we
produce?". Everything below either pins one half of that distinction or pins the discipline that
protects it — an immutable, content-addressed entry, an unpinned source refused at publish time
rather than flagged forever at pull time, and a pull that writes a model definition and touches
nothing else.

Structure mirrors ``tests/unit/test_agent_versions.py``: pure manifest checks first, then the
store, then the CLI against the real command tree.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examlops import catalog  # noqa: E402
from examlops.catalog import manifest as m  # noqa: E402
from examlops.catalog.pull import CatalogPullError, render_model_yaml  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data import catalog as store  # noqa: E402
from examlops.data.projects import create_project, list_project_resources  # noqa: E402

runner = CliRunner()

_ENTRY = {
    "name": "power-regression-recipe",
    "kind": "recipe",
    "description": "A node-power regression starting point.",
    "source": {"kind": "dataplane", "ref": "zenodo-pm100/pm100"},
    "license": "Apache-2.0",
    "resource_hint": "cpu-small",
    "recipe": {
        "model_yaml_template": "catalog/templates/power-regression.yaml",
        "defaults": {"model_class": "JPCP", "task_type": "regression", "framework": "sklearn"},
    },
}


def _entry(**overrides):
    doc = json.loads(json.dumps(_ENTRY))
    doc.update(overrides)
    return doc


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def models_dir(tmp_path) -> Path:
    d = tmp_path / "models"
    d.mkdir()
    return d


@pytest.fixture
def cli_env(tmp_path, monkeypatch, models_dir):
    """A hermetic CLI: this test's datastore, this test's models dir, no prompts."""
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("RAY_MODELS_DIR", str(models_dir))
    # Recipe templates are pack content, so a pull needs a pack — the shipped reference one,
    # named the way a deployment names it. `RAY_MODELS_DIR` still sends the *writes* to tmp_path.
    monkeypatch.setenv(
        "EXAMLOPS_USECASE_DIR", str(Path(__file__).resolve().parents[2] / "usecases" / "reference")
    )
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.delenv("EXAMLOPS_PRINCIPAL_KIND", raising=False)
    return models_dir


def _json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# ── the manifest: pure, content-addressed, strict ─────────────────────────────────────────


def test_entry_hash_is_stable_across_publishing_acts():
    """The hash covers the definition, not the act of publishing it.

    ADR 0158 writes ``entry_hash`` as "hash of every field below", and the two fields above it
    are ``name``/``catalog_version``. That reading is also the only one that works: if the hash
    covered the coordinates or the timestamp, no republish could ever be recognised as the same
    content, and "duplicate publish is idempotent" would be unimplementable.
    """
    a = m.normalize(_entry(published_by="alice", published_at="2026-01-01T00:00:00+00:00"))
    b = m.normalize(_entry(published_by="bob", published_at="2026-09-24T12:00:00+00:00"))
    assert m.entry_hash_of(a) == m.entry_hash_of(b)
    assert m.entry_hash_of(a).startswith("sha256:")
    assert len(m.entry_hash_of(a)) == len("sha256:") + 64


def test_a_changed_definition_is_a_different_hash():
    base = m.normalize(_entry())
    for change in (
        {"license": "MIT"},
        {"resource_hint": "gpu-1x-24gb"},
        {"source": {"kind": "dataplane", "ref": "other/spec"}},
        {"kind": "base_model", "recipe": None},
    ):
        other = m.normalize(_entry(**change))
        assert m.entry_hash_of(other) != m.entry_hash_of(base), change


def test_the_signature_is_over_the_identity_so_it_is_not_in_it():
    """``supplychain_ref`` cannot be part of what it signs, or the hash would be circular."""
    unsigned = m.normalize(_entry(provenance={"trust_tier": "T1_unsigned"}))
    signed = m.normalize(
        _entry(provenance={"trust_tier": "T1_signed", "supplychain_ref": "hmac-sha256:beef"})
    )
    other_sig = m.normalize(
        _entry(provenance={"trust_tier": "T1_signed", "supplychain_ref": "hmac-sha256:cafe"})
    )
    assert m.entry_hash_of(signed) == m.entry_hash_of(other_sig)
    # The tier itself IS hashed: signed and unsigned are not the same entry.
    assert m.entry_hash_of(signed) != m.entry_hash_of(unsigned)


def test_normalize_reports_every_problem_at_once():
    with pytest.raises(m.CatalogEntryError) as exc:
        m.normalize({"name": "BAD NAME", "kind": "nope", "license": "unknown"})
    problems = " ".join(exc.value.problems)
    assert "name" in problems
    assert "kind" in problems
    assert "license" in problems
    assert "source" in problems  # required and missing


@pytest.mark.parametrize(
    "ref",
    [
        "https://huggingface.co/distilbert/distilbert-base-uncased",  # no pointer at all
        "https://huggingface.co/distilbert/distilbert-base-uncased@main",  # a branch
        "https://example.org/model:latest",  # a floating tag
        "oci://ghcr.io/x/y:latest",
    ],
)
def test_an_unpinned_source_never_becomes_an_entry(ref):
    """ADR 0158 decision 3: refused at publish, not flagged forever at pull."""
    assert m.pinned_ref_problem(ref) is not None
    with pytest.raises(m.CatalogEntryError) as exc:
        m.normalize(_entry(source={"kind": "pinned_uri", "ref": ref}))
    assert any(m.TRUST_UNPINNED in p for p in exc.value.problems), exc.value.problems


@pytest.mark.parametrize(
    "ref",
    [
        "https://huggingface.co/distilbert/distilbert-base-uncased@" + "a" * 40,
        "https://example.org/artifact@sha256:" + "0" * 64,
        "oci://ghcr.io/mskazemi/examlops@sha256:" + "f" * 64,
        "https://zenodo.org/record/7541722",
    ],
)
def test_a_pinned_source_is_accepted(ref):
    assert m.pinned_ref_problem(ref) is None
    entry = m.normalize(_entry(source={"kind": "pinned_uri", "ref": ref}))
    assert entry["source"]["ref"] == ref


def test_an_unknown_license_is_refused_never_defaulted():
    for bad in ("unknown", "NOASSERTION", "n/a", "TBD"):
        with pytest.raises(m.CatalogEntryError) as exc:
            m.normalize(_entry(license=bad))
        assert any("license" in p for p in exc.value.problems)
    with pytest.raises(m.CatalogEntryError):
        m.normalize({k: v for k, v in _ENTRY.items() if k != "license"})


def test_a_recipe_without_a_template_is_refused():
    with pytest.raises(m.CatalogEntryError) as exc:
        m.normalize(_entry(recipe={"defaults": {}}))
    assert any("model_yaml_template" in p for p in exc.value.problems)


def test_a_declared_unpinned_trust_tier_is_refused():
    with pytest.raises(m.CatalogEntryError) as exc:
        m.normalize(_entry(provenance={"trust_tier": "unpinned"}))
    assert any("unpinned" in p for p in exc.value.problems)


# ── the store: insert-only, idempotent ────────────────────────────────────────────────────


def test_publishing_the_same_content_twice_is_idempotent():
    created, first = catalog.publish_entry(_entry(), actor="alice")
    assert created and first.catalog_version == 1
    created_again, second = catalog.publish_entry(_entry(), actor="bob")
    assert not created_again, "a duplicate publish must not allocate a new catalog_version"
    assert second.catalog_version == 1
    assert second.entry_hash == first.entry_hash
    assert second.published_by == "alice", "the original publish is not rewritten"
    assert len(store.list_entry_rows(latest_only=False)) == 1


def test_a_correction_is_a_new_version_never_a_rewrite():
    _, v1 = catalog.publish_entry(_entry(), actor="alice")
    created, v2 = catalog.publish_entry(_entry(license="MIT"), actor="alice")
    assert created and v2.catalog_version == 2
    assert v1.entry_hash != v2.entry_hash
    # v1 is still there, byte-identical to what was published.
    still = catalog.get_entry(f"{v1.name}@1")
    assert still is not None
    assert still.license == "Apache-2.0"
    assert still.entry_hash == v1.entry_hash
    assert catalog.get_entry(v1.name).catalog_version == 2, "bare name resolves to the newest"


def test_an_entry_record_is_frozen():
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    with pytest.raises(Exception):  # noqa: B017 - dataclasses.FrozenInstanceError
        entry.license = "MIT"  # type: ignore[misc]


def test_a_stored_entry_replays_its_own_hash():
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    row = store.get_entry_row(entry.name, entry.catalog_version)
    assert m.entry_hash_of(json.loads(row["manifest_json"])) == entry.entry_hash


def test_an_unsigned_publish_is_flagged_never_silently_equal_trust(monkeypatch):
    """No signing key configured: the entry publishes, as T1_unsigned, saying so."""
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY", raising=False)
    from examlops import supplychain

    monkeypatch.setattr(supplychain, "sign_or_explain", lambda payload, *, subject: (None, None))
    _, entry = catalog.publish_entry(_entry(), actor="alice", sign=True)
    assert entry.trust_tier == "T1_unsigned"
    assert entry.supplychain_ref is None
    assert entry.signed is False
    assert entry.to_dict()["provenance"]["trust_tier"] == "T1_unsigned"


def test_a_signed_publish_records_the_supplychain_signature(monkeypatch):
    from examlops import supplychain

    monkeypatch.setattr(
        supplychain, "sign_or_explain", lambda payload, *, subject: ("deadbeef", "hmac-sha256")
    )
    _, entry = catalog.publish_entry(_entry(), actor="alice", sign=True)
    assert entry.trust_tier == "T1_signed"
    assert entry.supplychain_ref == "hmac-sha256:deadbeef"
    assert entry.signed is True


def test_a_curator_cannot_self_declare_a_signature():
    """A trust tier the platform did not produce is not honoured."""
    _, entry = catalog.publish_entry(
        _entry(provenance={"trust_tier": "T1_signed", "supplychain_ref": "i-signed-it-myself"}),
        actor="mallory",
    )
    assert entry.trust_tier == "T1_unsigned"
    assert entry.supplychain_ref is None


# ── verify_entry_signature: the verify-before-load-shaped gate (ADR 0158 decision 1) ───────


def test_a_genuinely_signed_entry_verifies(monkeypatch):
    """The real signer, not a mock — proves the recomputed HMAC actually matches what was signed,
    not merely that the two code paths agree with each other on a fake value."""
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "a-real-test-signing-key")
    _, entry = catalog.publish_entry(_entry(), actor="alice", sign=True)
    assert entry.trust_tier == "T1_signed"
    assert catalog.verify_entry_signature(entry.ref) == "verified"


def test_an_unsigned_entry_has_nothing_to_verify():
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    assert entry.trust_tier == "T1_unsigned"
    assert catalog.verify_entry_signature(entry.ref) == "unsigned"


def test_a_signed_entry_is_unverifiable_with_no_key_configured_on_this_host(monkeypatch):
    """Signed elsewhere, read here with no key — reported, not silently treated as a pass."""
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "the-signing-key")
    _, entry = catalog.publish_entry(_entry(), actor="alice", sign=True)
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY", raising=False)
    assert catalog.verify_entry_signature(entry.ref) == "unverifiable"


def test_a_tampered_signed_entry_is_refused_not_reported_as_verified(monkeypatch):
    """The row was edited in the datastore after signing — caught, always, never a quiet pass."""
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "a-real-test-signing-key")
    _, entry = catalog.publish_entry(_entry(), actor="alice", sign=True)
    manifest = json.loads(store.get_entry_row(entry.name, entry.catalog_version)["manifest_json"])
    manifest["license"] = "mit"
    from examlops.data import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE catalog_entries SET manifest_json=? WHERE name=? AND catalog_version=?",
            (json.dumps(manifest), entry.name, entry.catalog_version),
        )
        conn.commit()
    with pytest.raises(catalog.CatalogSignatureError, match="does not match"):
        catalog.verify_entry_signature(entry.ref)


def test_a_stripped_supplychain_ref_on_a_claimed_t1_signed_row_is_refused():
    """T1_signed with no supplychain_ref at all is not "unsigned" — it is a broken claim."""
    from examlops.data import get_db

    _, entry = catalog.publish_entry(_entry(), actor="alice")  # T1_unsigned
    with get_db() as conn:
        conn.execute(
            "UPDATE catalog_entries SET trust_tier='T1_signed' WHERE name=? AND catalog_version=?",
            (entry.name, entry.catalog_version),
        )
        conn.commit()
    with pytest.raises(catalog.CatalogSignatureError, match="no supplychain_ref"):
        catalog.verify_entry_signature(entry.ref)


def test_listing_filters_without_registry_concepts():
    catalog.publish_entry(_entry(), actor="a")
    catalog.publish_entry(
        _entry(name="bare-base", kind="base_model", recipe=None, license="MIT"), actor="a"
    )
    assert {e.name for e in catalog.list_entries()} == {"power-regression-recipe", "bare-base"}
    assert [e.name for e in catalog.list_entries(kind="base_model")] == ["bare-base"]
    assert [e.name for e in catalog.list_entries(license_id="mit")] == ["bare-base"]
    assert catalog.list_entries(evaluated_only=True) == []
    assert [e.name for e in catalog.list_entries(trust_tier="T1_unsigned")] == [
        "bare-base",
        "power-regression-recipe",
    ]


def test_an_entry_with_no_eval_summary_is_flagged_not_assumed_good():
    _, entry = catalog.publish_entry(_entry(), actor="a")
    assert entry.evaluated is False
    _, evaluated = catalog.publish_entry(
        _entry(name="scored", eval_summary_ref={"suite": "smoke@1", "model_version": "scored@1"}),
        actor="a",
    )
    assert evaluated.evaluated is True
    assert evaluated.eval_suite == "smoke@1"


# ── the pull: it writes a model definition and nothing else ───────────────────────────────


def test_pull_materializes_a_yaml_the_real_loader_accepts(models_dir, repo_root, monkeypatch):
    """The acceptance criterion of Phase 2: the rendered file is a per-model YAML, full stop."""
    # The template path is pack-relative; with no pack configured the pack seam's default,
    # `usecases/reference`, is itself CWD-relative — so the pack is found from the repo root.
    monkeypatch.chdir(repo_root)
    from pipelines.model_loader import load_model_yaml

    create_project("research")
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    result = catalog.pull_entry(
        entry, "research", model_name="MyPower", actor="alice", models_dir=models_dir
    )

    path = Path(result.path)
    assert path.is_file()
    loaded = load_model_yaml(path)
    assert loaded.name == "MyPower"
    assert loaded.project == "research"
    assert loaded.task_type == "regression"
    assert loaded.config_class
    assert loaded.serving["model_id"] == "mypower"
    assert loaded.enabled is False, "nothing has been trained, so nothing should be scheduled"
    assert [d.name for d in loaded.datasets] == ["PM100Dataset"]
    # The provenance an operator needs is in the file they will edit.
    text = path.read_text()
    assert entry.entry_hash in text
    assert "exa catalog pull" in text


def test_a_base_model_entry_renders_a_loader_valid_default(models_dir):
    from pipelines.model_loader import load_model_yaml

    create_project("research")
    _, entry = catalog.publish_entry(
        _entry(name="bare-base", kind="base_model", recipe=None), actor="alice"
    )
    result = catalog.pull_entry(entry, "research", actor="alice", models_dir=models_dir)
    loaded = load_model_yaml(Path(result.path))
    assert loaded.name == "bare-base"
    assert loaded.enabled is False
    assert loaded.datasets == []


def test_pull_records_membership_lineage_and_the_pull_row(models_dir, repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    from examlops.data.events import lineage_graph

    create_project("research")
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    result = catalog.pull_entry(
        entry, "research", model_name="MyPower", actor="alice", models_dir=models_dir
    )

    # 1. project_resources membership through the existing join (ADR 0086), no new table.
    assert "MyPower" in list_project_resources("research")["model"]

    # 2. exactly one catalog_pulls row, naming the immutable entry it came from.
    pulls = catalog.list_pulls(project="research")
    assert len(pulls) == 1
    assert pulls[0]["entry"] == entry.name
    assert pulls[0]["catalog_version"] == entry.catalog_version
    assert pulls[0]["entry_hash"] == entry.entry_hash
    assert pulls[0]["model_name"] == "MyPower"

    # 3. a lineage edge catalog_entry -> model, through the platform's only lineage system.
    graph = lineage_graph("MyPower")
    assert graph["runs"], "the pull emitted no lineage run"
    upstream = {(n["node_type"], n["node_name"]) for n in graph["upstream"]}
    assert ("catalog_entry", f"examlops://catalog_entry/{entry.ref}") in upstream
    downstream = {n["node_name"] for n in graph["downstream"]}
    assert "examlops://model/MyPower" in downstream
    assert result.lineage_edge.startswith(f"examlops://catalog_entry/{entry.ref}")


def test_dry_run_writes_nothing_at_all(models_dir, repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    create_project("research")
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    before = store.count_pulls()

    result = catalog.pull_entry(
        entry, "research", model_name="MyPower", actor="alice", dry_run=True, models_dir=models_dir
    )

    assert result.dry_run is True
    assert result.rendered, "a dry run still shows what WOULD be written"
    assert not Path(result.path).exists()
    assert store.count_pulls() == before
    assert list_project_resources("research").get("model", []) == []


def test_pull_never_overwrites_an_existing_definition(models_dir, repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    create_project("research")
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    catalog.pull_entry(entry, "research", model_name="MyPower", models_dir=models_dir)
    with pytest.raises(CatalogPullError, match="already exists"):
        catalog.pull_entry(entry, "research", model_name="MyPower", models_dir=models_dir)


def test_pull_into_a_missing_project_is_refused(models_dir, repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    with pytest.raises(CatalogPullError, match="does not exist"):
        catalog.pull_entry(entry, "no-such-project", models_dir=models_dir)


def test_pull_of_a_genuinely_signed_entry_succeeds(models_dir, repo_root, monkeypatch):
    """ADR 0158 decision 1's verify-before-load-shaped gate does not block a real signature."""
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "a-real-test-signing-key")
    create_project("research")
    _, entry = catalog.publish_entry(_entry(), actor="alice", sign=True)
    assert entry.trust_tier == "T1_signed"
    result = catalog.pull_entry(
        entry, "research", model_name="MyPower", actor="alice", models_dir=models_dir
    )
    assert Path(result.path).is_file()
    assert result.trust_tier == "T1_signed"


def test_pull_refuses_a_tampered_signed_entry(models_dir, repo_root, monkeypatch):
    """The one thing decision 1 exists to catch: a T1_signed row that does not actually verify."""
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "a-real-test-signing-key")
    create_project("research")
    _, entry = catalog.publish_entry(_entry(), actor="alice", sign=True)
    manifest = json.loads(store.get_entry_row(entry.name, entry.catalog_version)["manifest_json"])
    manifest["license"] = "mit"
    from examlops.data import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE catalog_entries SET manifest_json=? WHERE name=? AND catalog_version=?",
            (json.dumps(manifest), entry.name, entry.catalog_version),
        )
        conn.commit()
    # Re-read: pull_entry needs the current, live CatalogEntry, not the stale one from publish.
    fresh = catalog.get_entry(entry.ref)
    with pytest.raises(CatalogPullError, match="does not match"):
        catalog.pull_entry(fresh, "research", model_name="MyPower", models_dir=models_dir)
    assert not (models_dir / "mypower.yaml").exists(), "a refused pull must write nothing"


def test_dry_run_also_refuses_a_tampered_signed_entry(models_dir, repo_root, monkeypatch):
    """A dry-run preview that omits the one check that can refuse the real pull is not accurate."""
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "a-real-test-signing-key")
    create_project("research")
    _, entry = catalog.publish_entry(_entry(), actor="alice", sign=True)
    manifest = json.loads(store.get_entry_row(entry.name, entry.catalog_version)["manifest_json"])
    manifest["license"] = "mit"
    from examlops.data import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE catalog_entries SET manifest_json=? WHERE name=? AND catalog_version=?",
            (json.dumps(manifest), entry.name, entry.catalog_version),
        )
        conn.commit()
    fresh = catalog.get_entry(entry.ref)
    with pytest.raises(CatalogPullError, match="does not match"):
        catalog.pull_entry(
            fresh, "research", model_name="MyPower", dry_run=True, models_dir=models_dir
        )


def test_an_unsigned_entry_is_flagged_in_the_file_it_writes(models_dir):
    create_project("research")
    _, entry = catalog.publish_entry(
        _entry(name="bare-base", kind="base_model", recipe=None), actor="alice"
    )
    rendered = render_model_yaml(entry, "BareBase", "research")
    assert "unsigned source" in rendered.lower()
    assert "trust_tier: T1_unsigned" in rendered


def test_a_template_cannot_mis_name_the_model_it_was_pulled_as(tmp_path, monkeypatch):
    template = tmp_path / "t.yaml"
    template.write_text(
        "name: WRONG\nconfig_class: x.Y\ntask_type: regression\n"
        "serving:\n  model_id: wrong\n  aliases: [Production]\n"
    )
    monkeypatch.setenv("EXAMLOPS_CATALOG_TEMPLATE_DIR", str(tmp_path))
    _, entry = catalog.publish_entry(
        _entry(recipe={"model_yaml_template": "t.yaml"}), actor="alice"
    )
    doc = yaml.safe_load(render_model_yaml(entry, "Right", "research"))
    assert doc["name"] == "Right"
    assert doc["serving"]["model_id"] == "right"
    assert doc["project"] == "research"


def _template(root: Path) -> Path:
    path = root / "catalog" / "templates" / "power-regression.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("name: ${model_name}\nconfig_class: x.Y\ntask_type: regression\n")
    return path


def test_a_recipe_template_is_pack_content_not_repository_content(tmp_path, monkeypatch):
    """A template is found through the pack seam and the data root — never via the checkout.

    A wheel or an image has no repository around it (ADR 0129 §8), so the three roots are the
    explicit override, the active use-case pack (wherever it is), and the site's own config dir.
    Here the pack is a bare directory in ``tmp_path`` with nothing else in it: if resolution ever
    reached back out to the checkout, the pack-relative path would still be found and this would
    stop proving anything — so the run with no root configured at all is asserted to fail.
    """
    from examlops.catalog import pull as pull_mod

    monkeypatch.delenv("EXAMLOPS_CATALOG_TEMPLATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # no checkout under the CWD either
    _, entry = catalog.publish_entry(_entry(), actor="alice")

    # 1. the active use-case pack, however the deployment points at one
    pack = tmp_path / "pack"
    _template(pack)
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(pack))
    assert yaml.safe_load(render_model_yaml(entry, "FromPack", "research"))["name"] == "FromPack"

    # 2. the site's own templates under the ADR 0128 data root, i.e. <config dir>/catalog —
    #    here reached with this entry's own pack-shaped relative path
    monkeypatch.delenv("EXAMLOPS_USECASE_DIR")
    site = tmp_path / "data"
    _template(site / "config" / "catalog")
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(site))
    assert yaml.safe_load(render_model_yaml(entry, "FromSite", "research"))["name"] == "FromSite"

    # 3. nothing configured: a miss that names what would fix it, never a silent repo lookup
    monkeypatch.delenv("EXAMLOPS_DATA_DIR")
    monkeypatch.setenv("EXAMLOPS_CONFIG_DIR", str(tmp_path / "empty"))
    with pytest.raises(CatalogPullError, match="was not found"):
        render_model_yaml(entry, "Nowhere", "research")
    assert not any(
        "examlops" in p.parts and "catalog" in p.parts for p in pull_mod._template_roots()
    ), "the package's own directory is not a template root"


# ── the distinction ADR 0158 decision 4 exists to protect ─────────────────────────────────


def test_a_pull_is_not_a_registration(models_dir, repo_root, monkeypatch):
    """The catalog is NOT the registry: a pull creates no MLflow registered model.

    This is the single most likely design mistake, so it is asserted rather than assumed: the
    whole ``mlflow`` package is replaced with an object that raises on ANY attribute access, and
    a full pull runs through it untouched. Nothing here may create a version, move an alias or
    start a replica — and a future edit that reached for the registry would fail here.
    """
    monkeypatch.chdir(repo_root)

    class _Forbidden:
        def __getattr__(self, name):
            raise AssertionError(
                f"exa catalog pull reached MLflow (mlflow.{name}) — a catalog entry is a "
                "starting point, not a registry version (ADR 0158 decision 4)"
            )

    for mod in ("mlflow", "mlflow.tracking", "mlflow.client"):
        monkeypatch.setitem(sys.modules, mod, _Forbidden())

    create_project("research")
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    result = catalog.pull_entry(
        entry, "research", model_name="MyPower", actor="alice", models_dir=models_dir
    )

    payload = result.to_dict()
    assert payload["registry_version_created"] is False
    assert payload["trained"] is False
    assert payload["served"] is False
    # An entry carries none of the registry's vocabulary.
    assert not hasattr(entry, "alias")
    assert not hasattr(entry, "stage")
    assert "alias" not in entry.to_dict()
    assert entry.to_dict()["is_registry_model"] is False


def test_a_catalog_entry_is_addressed_by_version_not_by_alias():
    _, entry = catalog.publish_entry(_entry(), actor="alice")
    assert entry.ref == f"{entry.name}@1"
    with pytest.raises(m.CatalogEntryError, match="integer"):
        catalog.resolve_ref(f"{entry.name}@Production")
    assert catalog.get_entry(f"{entry.name}@99") is None


# ── the CLI ───────────────────────────────────────────────────────────────────────────────


def test_cli_list_and_show_json_shape(cli_env):
    catalog.publish_entry(_entry(), actor="alice")

    rows = _json(runner.invoke(app, ["--output", "json", "catalog", "list"]))
    assert isinstance(rows, list) and len(rows) == 1
    row = rows[0]
    assert row["name"] == "power-regression-recipe"
    assert row["catalog_version"] == 1
    assert row["entry_hash"].startswith("sha256:")
    assert row["kind"] == "recipe"
    assert row["license"] == "Apache-2.0"
    assert row["provenance"]["trust_tier"] == "T1_unsigned"
    assert row["source"] == {"kind": "dataplane", "ref": "zenodo-pm100/pm100"}
    assert row["evaluated"] is False
    assert row["is_registry_model"] is False

    shown = _json(
        runner.invoke(app, ["--output", "json", "catalog", "show", "power-regression-recipe@1"])
    )
    assert shown["entry_hash"] == row["entry_hash"]
    assert shown["ref"] == "power-regression-recipe@1"


def test_cli_publish_refuses_an_unpinned_source_with_a_named_reason(cli_env, tmp_path):
    path = tmp_path / "entry.yaml"
    path.write_text(
        yaml.safe_dump(
            _entry(source={"kind": "pinned_uri", "ref": "https://example.org/model@main"})
        )
    )
    result = runner.invoke(app, ["catalog", "publish", str(path)])
    assert result.exit_code == 1, result.output
    assert "unpinned" in result.output
    assert store.list_entry_rows() == [], "a refused entry must not reach storage"


def test_cli_publish_then_pull_end_to_end(cli_env, tmp_path, repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("RAY_MODELS_DIR", str(cli_env))
    create_project("research")
    entry_file = tmp_path / "entry.yaml"
    entry_file.write_text(yaml.safe_dump(_entry()))

    published = _json(
        runner.invoke(app, ["--output", "json", "catalog", "publish", str(entry_file)])
    )
    assert published["created"] is True
    assert published["provenance"]["trust_tier"] == "T1_unsigned"

    preview = _json(
        runner.invoke(
            app,
            [
                "--output",
                "json",
                "catalog",
                "pull",
                "power-regression-recipe",
                "--project",
                "research",
                "--as",
                "MyPower",
                "--dry-run",
            ],
        )
    )
    assert preview["dry_run"] is True
    assert not Path(preview["path"]).exists()

    pulled = _json(
        runner.invoke(
            app,
            [
                "--output",
                "json",
                "catalog",
                "pull",
                "power-regression-recipe",
                "--project",
                "research",
                "--as",
                "MyPower",
                "--yes",
            ],
        )
    )
    assert pulled["dry_run"] is False
    assert pulled["registry_version_created"] is False
    assert pulled["unsigned_source"] is True
    assert Path(pulled["path"]).is_file()
    assert "MyPower" in list_project_resources("research")["model"]


def test_cli_pull_of_an_unknown_entry_exits_non_zero(cli_env):
    result = runner.invoke(
        app, ["catalog", "pull", "no-such-entry", "--project", "research", "--yes"]
    )
    assert result.exit_code != 0
    assert "no-such-entry" in result.output


def test_cli_show_of_an_unknown_entry_exits_non_zero(cli_env):
    result = runner.invoke(app, ["catalog", "show", "no-such-entry"])
    assert result.exit_code != 0


def test_cli_mutations_are_audited(cli_env, tmp_path, repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("RAY_MODELS_DIR", str(cli_env))
    from examlops.data.audit import export_audit_events

    create_project("research")
    entry_file = tmp_path / "entry.yaml"
    entry_file.write_text(yaml.safe_dump(_entry()))
    assert runner.invoke(app, ["catalog", "publish", str(entry_file)]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["catalog", "pull", "power-regression-recipe", "--project", "research", "--yes"],
        ).exit_code
        == 0
    )
    actions = {e["action"] for e in export_audit_events()}
    assert {"catalog_publish", "catalog_pull"} <= actions


def test_the_shipped_seed_entry_publishes_and_pulls(cli_env, repo_root, monkeypatch):
    """The in-repo curated entry is real: it validates, and what it renders loads."""
    monkeypatch.chdir(repo_root)
    from pipelines.model_loader import load_model_yaml

    seed = repo_root / "usecases/reference/catalog/entries/power-regression-recipe.yaml"
    doc = yaml.safe_load(seed.read_text())
    _, entry = catalog.publish_entry(doc, actor="curator")
    create_project("research")
    result = catalog.pull_entry(entry, "research", model_name="SeedPower", models_dir=cli_env)
    assert load_model_yaml(Path(result.path)).name == "SeedPower"
