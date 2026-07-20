"""B6 — embedding lifecycle & reindexing (ADR 0043)."""

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


def test_r1_register_encoder_deterministic_id():
    from examlops.embeddings import register_encoder

    a = register_encoder("nomic-embed-text", "v1.5", 768)
    b = register_encoder("nomic-embed-text", "v1.5", 768)
    assert a == b  # content-addressed
    c = register_encoder("nomic-embed-text", "v2.0", 768)
    assert c != a


def test_gwt1_encoder_id_stamped_and_retrievable():
    from examlops.embeddings import register_encoder
    from examlops.platform_db import get_encoder

    eid = register_encoder("e5", "v1", 1024, metric="dot")
    row = get_encoder(eid)
    assert row["dim"] == 1024
    assert row["metric"] == "dot"


def test_gwt2_cross_encoder_compare_refused():
    from examlops.embeddings import EncoderMismatchError, guard_compatible

    guard_compatible("enc-a", "enc-a")  # same → ok
    with pytest.raises(EncoderMismatchError):
        guard_compatible("enc-a", "enc-b")


def test_gwt3_reindex_verified_atomic_switch():
    from examlops.embeddings import register_encoder, reindex, set_collection_encoder
    from examlops.platform_db import get_collection

    old = register_encoder("e5", "v1", 768)
    new = register_encoder("e5", "v2", 768)
    set_collection_encoder("docs", old)
    result = reindex("docs", new, corpus_size=1000, recall_fn=lambda: 0.97, recall_floor=0.9)
    assert result.switched is True
    coll = get_collection("docs")
    assert coll["active_encoder_id"] == new
    assert coll["staging_encoder_id"] is None  # pruned after switch (GWT-4)
    assert coll["status"] == "active"


def test_gwt3_reindex_low_recall_aborts_keeps_old():
    from examlops.embeddings import register_encoder, reindex, set_collection_encoder
    from examlops.platform_db import get_collection

    old = register_encoder("e5", "v1", 768)
    new = register_encoder("e5", "v2", 768)
    set_collection_encoder("docs", old)
    result = reindex("docs", new, corpus_size=1000, recall_fn=lambda: 0.5, recall_floor=0.9)
    assert result.switched is False
    coll = get_collection("docs")
    assert coll["active_encoder_id"] == old  # kept old (R5/GWT-4)
    assert coll["staging_encoder_id"] is None


def test_reindex_unknown_encoder_rejected():
    from examlops.embeddings import reindex

    with pytest.raises(ValueError):
        reindex("docs", "no-such-encoder")


def test_gwt5_reindex_rebaselines_input_drift():
    from examlops.embeddings import register_encoder, reindex, set_collection_encoder
    from examlops.platform_db import get_input_baseline

    old = register_encoder("e5", "v1", 768)
    new = register_encoder("e5", "v2", 768)
    set_collection_encoder("docs", old)
    reindex("docs", new, recall_fn=lambda: 1.0)
    baseline = get_input_baseline("docs")
    assert baseline is not None
    assert baseline.get("rebaselined_for_encoder") == new


def test_gwt6_reindex_audited_and_tenant_scoped():
    from examlops.embeddings import register_encoder, reindex, set_collection_encoder
    from examlops.platform_db import get_db

    old = register_encoder("e5", "v1", 768)
    new = register_encoder("e5", "v2", 768)
    set_collection_encoder("docs", old, tenant="acme")
    reindex("docs", new, tenant="acme", recall_fn=lambda: 1.0)
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM audit_events WHERE action='reindex_switched'").fetchall()
    assert len(rows) == 1
    assert rows[0]["tenant"] == "acme"


def test_per_tenant_collections_independent():
    from examlops.embeddings import register_encoder, set_collection_encoder
    from examlops.platform_db import get_collection

    e1 = register_encoder("e5", "v1", 768)
    e2 = register_encoder("e5", "v2", 768)
    set_collection_encoder("docs", e1, tenant="a")
    set_collection_encoder("docs", e2, tenant="b")
    assert get_collection("docs", "a")["active_encoder_id"] == e1
    assert get_collection("docs", "b")["active_encoder_id"] == e2


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r = runner.invoke(app, ["embedding", "register", "nomic", "v1", "--dim", "768"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["embedding", "list"])
    assert r.exit_code == 0, r.output
    # Grab the registered id and reindex.
    from examlops.embeddings import register_encoder, set_collection_encoder

    e1 = register_encoder("nomic", "v1", 768)
    e2 = register_encoder("nomic", "v2", 768)
    set_collection_encoder("docs", e1)
    r = runner.invoke(
        app, ["embedding", "reindex", "docs", e2, "--corpus-size", "100", "--recall", "0.99"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["embedding", "status", "docs"])
    assert r.exit_code == 0, r.output
