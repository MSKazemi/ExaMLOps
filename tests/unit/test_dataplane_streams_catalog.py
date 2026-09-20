"""Stream catalog + bindings + tenancy (ADR 0130/0131, Plan 2 batch S1, task A6).

Covers:

* CRUD on the ``dataplane_streams`` catalog (``examlops.data.dataplane``), including the
  origin-conditional upsert and the conditional ``set_stream_state``/``state_reason``;
* ``examlops.dataplane.streams.bindings``: YAML parsing (incl. the legacy ``dataplane_bus_uuid``
  shim, off by default), tenancy (ruling R11) with ASCII name validation (fix round 1 #3),
  canonical model-spelling resolution (fix round 1 #4), origin ownership (fix round 1 #1), the
  safe removal sweep (fix round 1 #2), per-entry robustness (fix round 1 #5), and
  ``define_stream``'s audit trail;
* parity between ``bindings.yaml_streams`` and the dataplane-import-free
  ``pipelines.model_loader.normalize_inference_streams``.
"""

from __future__ import annotations

import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops.data import dataplane as catalog  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.data.projects import create_project  # noqa: E402
from examlops.dataplane.streams import bindings  # noqa: E402
from examlops.dataplane.streams.types import StreamBinding, StreamLimits  # noqa: E402
from examlops.dataplane.types import SpecError  # noqa: E402
from pipelines.model_loader import ModelYAMLConfig, normalize_inference_streams  # noqa: E402

_LEGACY_ENV = "EXAMLOPS_DATAPLANE_LEGACY_DATAPLANE_BUS_UUID"
_KELVIN = "K"  # KELVIN SIGN — Python str.lower() folds it to 'k', SQLite's lower() does not
_FULLWIDTH_J = "Ｊ"  # FULLWIDTH LATIN CAPITAL LETTER J — non-ASCII lookalike of 'J'


def _assign_model(project: str, model: str) -> None:
    """Assign *model* to *project* the way ``exa project assign --kind model`` does."""
    from examlops.data import get_db, init_db

    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO project_resources (project, kind, ref) VALUES (?, 'model', ?)",
            (project, model),
        )


@pytest.fixture
def pack(tmp_path, monkeypatch) -> Path:
    """An isolated, empty use-case pack — every ``define_stream``/``sync_pack_streams`` test uses
    this instead of the real ``usecases/reference`` pack, so (a) tests never depend on, or race
    with, another session editing that pack's YAML files, and (b) ``define_stream``'s canonical
    model-name resolution (fix round 1 #4) only ever sees models the test itself declared."""
    root = tmp_path / "pack"
    (root / "models").mkdir(parents=True)
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(root))
    return root / "models"


def _write_model(models_dir: Path, name: str, **extra: Any) -> None:
    doc: dict[str, Any] = {"name": name, **extra}
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.lower())
    (models_dir / f"{safe}.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True))


# ── catalog CRUD (examlops.data.dataplane) ──────────────────────────────────


def test_upsert_get_list_delete_stream_roundtrip():
    assert (
        catalog.upsert_stream(
            "research",
            "s1",
            connector="http",
            model="JPCP",
            alias="Production",
            address="https://example/infer",
            connection="conn-1",
            options={"passthrough": ["num_nodes"]},
            limits={"max_in_flight": 8},
            state="enabled",
            origin="api",
            actor="alice",
        )
        is True
    )
    row = catalog.get_stream("s1", "research")
    assert row is not None
    assert row["connector"] == "http"
    assert row["model"] == "JPCP"
    assert row["alias"] == "Production"
    assert row["options"] == {"passthrough": ["num_nodes"]}
    assert row["limits"] == {"max_in_flight": 8}
    assert row["state"] == "enabled"
    assert row["origin"] == "api"
    assert row["created_by"] == "alice"
    assert row["state_reason"] is None

    assert catalog.get_stream("does-not-exist", "research") is None

    all_rows = catalog.list_streams()
    assert [r["name"] for r in all_rows] == ["s1"]
    scoped = catalog.list_streams("research")
    assert [r["name"] for r in scoped] == ["s1"]
    assert catalog.list_streams("other-project") == []

    assert catalog.delete_stream("s1", "research") is True
    assert catalog.get_stream("s1", "research") is None
    assert catalog.delete_stream("s1", "research") is False


def test_upsert_stream_reupsert_preserves_state_but_refreshes_definition():
    catalog.upsert_stream(
        "",
        "s2",
        connector="http",
        model="JPCP",
        alias="Production",
        address="https://old",
        connection=None,
        options={},
        limits={},
        state="enabled",
        origin="api",
        actor="alice",
    )
    assert catalog.set_stream_state("", "s2", "paused") is True

    # Re-upsert (same origin) with a different address/connector; state must survive untouched.
    catalog.upsert_stream(
        "",
        "s2",
        connector="kafka",
        model="JPCP",
        alias="Canary",
        address="topic-1",
        connection="kconn",
        options={"x": 1},
        limits={"max_bytes": 999},
        state="enabled",  # deliberately different from the current stored state
        origin="api",
        actor="bob",
    )
    row = catalog.get_stream("s2", "")
    assert row is not None
    assert row["connector"] == "kafka"
    assert row["address"] == "topic-1"
    assert row["state"] == "paused"  # preserved, not reset to "enabled"


def test_upsert_stream_rejects_invalid_state():
    with pytest.raises(ValueError):
        catalog.upsert_stream(
            "",
            "bad",
            connector="http",
            model="JPCP",
            alias="Production",
            address="",
            connection=None,
            options={},
            limits={},
            state="not-a-state",
            origin="api",
            actor=None,
        )


def test_upsert_stream_refuses_conflicting_origin():
    """Fix round 1, finding 1 — the origin guard lives in the upsert itself."""
    catalog.upsert_stream(
        "",
        "s-origin",
        connector="http",
        model="JPCP",
        alias="Production",
        address="pack-address",
        connection=None,
        options={},
        limits={},
        state="enabled",
        origin="pack",
        actor="ci",
    )
    # An "api" upsert on the same (project, name) must be refused — no columns change.
    ok = catalog.upsert_stream(
        "",
        "s-origin",
        connector="kafka",
        model="OTHER",
        alias="Canary",
        address="api-address",
        connection="c",
        options={"x": 1},
        limits={},
        state="enabled",
        origin="api",
        actor="alice",
    )
    assert ok is False
    row = catalog.get_stream("s-origin", "")
    assert row["origin"] == "pack"
    assert row["address"] == "pack-address"  # untouched by the refused write
    assert row["connector"] == "http"

    # The reverse direction is refused too.
    ok2 = catalog.upsert_stream(
        "",
        "s-origin",
        connector="grpc",
        model="OTHER2",
        alias="Staging",
        address="another",
        connection=None,
        options={},
        limits={},
        state="enabled",
        origin="pack",  # matches existing origin -> allowed
        actor="ci",
    )
    assert ok2 is True
    assert catalog.get_stream("s-origin", "")["connector"] == "grpc"


def test_set_stream_state_conditional_update_and_reason():
    catalog.upsert_stream(
        "",
        "s3",
        connector="http",
        model="JPCP",
        alias="Production",
        address="",
        connection=None,
        options={},
        limits={},
        state="enabled",
        origin="api",
        actor=None,
    )
    # Stale-read guard: only_if_state that doesn't match -> no-op, returns False.
    assert catalog.set_stream_state("", "s3", "disabled", only_if_state="paused") is False
    assert catalog.get_stream("s3", "")["state"] == "enabled"

    # Matching only_if_state -> applies, and reason is recorded.
    assert (
        catalog.set_stream_state("", "s3", "disabled", only_if_state="enabled", reason="r1") is True
    )
    row = catalog.get_stream("s3", "")
    assert row["state"] == "disabled"
    assert row["state_reason"] == "r1"

    # Re-enabling with no reason clears it.
    assert catalog.set_stream_state("", "s3", "enabled") is True
    assert catalog.get_stream("s3", "")["state_reason"] is None

    # Unconditional update on a nonexistent row -> False.
    assert catalog.set_stream_state("", "does-not-exist", "enabled") is False

    with pytest.raises(ValueError):
        catalog.set_stream_state("", "s3", "bogus")
    with pytest.raises(ValueError):
        catalog.set_stream_state("", "s3", "enabled", only_if_state="bogus")


# ── bindings.yaml_streams (pure YAML parsing) ───────────────────────────────


def test_yaml_streams_parses_entries_with_defaults():
    model_yaml = {
        "name": "JPCP",
        "inference": {
            "streams": [
                {"name": "http-in", "connector": "http", "address": "https://x/infer"},
                {
                    "name": "kafka-in",
                    "connector": "kafka",
                    "address": "topic-a",
                    "alias": "Canary",
                    "connection": "kconn",
                    "options": {"passthrough": ["a"]},
                    "limits": {"max_in_flight": 16},
                },
            ]
        },
    }
    result = bindings.yaml_streams(model_yaml, project="research")
    assert [b.name for b in result] == ["http-in", "kafka-in"]

    http_b = result[0]
    assert http_b.project == "research"
    assert http_b.model == "JPCP"
    assert http_b.alias == "Production"  # default
    assert http_b.connection is None
    assert http_b.options == {}
    assert http_b.limits == StreamLimits()  # all defaults

    kafka_b = result[1]
    assert kafka_b.alias == "Canary"
    assert kafka_b.connection == "kconn"
    assert kafka_b.options == {"passthrough": ["a"]}
    assert kafka_b.limits == StreamLimits(max_in_flight=16)


def test_yaml_streams_global_project_normalizes_alias():
    model_yaml = {"name": "JPCP", "inference": {"streams": []}}
    assert bindings.yaml_streams(model_yaml, project="_global") == []
    b = bindings.yaml_streams(
        {"name": "JPCP", "inference": {"streams": [{"name": "s", "connector": "http"}]}},
        project="_global",
    )[0]
    assert b.project == ""  # "_global" and "" are the same stored value


def test_yaml_streams_requires_name_and_connector():
    with pytest.raises(SpecError):
        bindings.yaml_streams(
            {"name": "JPCP", "inference": {"streams": [{"connector": "http"}]}}, project=""
        )
    with pytest.raises(SpecError):
        bindings.yaml_streams({"inference": {"streams": []}}, project="")


def test_yaml_streams_rejects_bad_state():
    with pytest.raises(SpecError):
        bindings.yaml_streams(
            {
                "name": "JPCP",
                "inference": {"streams": [{"name": "s", "connector": "http", "state": "Enabled"}]},
            },
            project="",
        )


def test_yaml_streams_rejects_non_dict_entry():
    with pytest.raises(SpecError):
        bindings.yaml_streams(
            {"name": "JPCP", "inference": {"streams": ["not-a-dict"]}}, project=""
        )


def test_yaml_streams_rejects_non_ascii_stream_name():
    with pytest.raises(SpecError):
        bindings.yaml_streams(
            {
                "name": "JPCP",
                "inference": {"streams": [{"name": f"s{_KELVIN}", "connector": "http"}]},
            },
            project="",
        )


def test_yaml_streams_rejects_non_ascii_model():
    with pytest.raises(SpecError):
        bindings.yaml_streams({"name": f"JPCP{_KELVIN}", "inference": {}}, project="")


def test_yaml_streams_legacy_dataplane_bus_off_by_default(monkeypatch):
    monkeypatch.delenv(_LEGACY_ENV, raising=False)
    model_yaml = {"name": "JPCP", "dataplane_bus_uuid": "abc-123", "inference": {}}
    assert bindings.yaml_streams(model_yaml, project="") == []


def test_yaml_streams_legacy_dataplane_bus_enabled_via_env(monkeypatch):
    monkeypatch.setenv(_LEGACY_ENV, "1")
    model_yaml = {"name": "JPCP", "dataplane_bus_uuid": "abc-123", "inference": {}}
    result = bindings.yaml_streams(model_yaml, project="")
    assert len(result) == 1
    b = result[0]
    assert b.connector == "dataplane-bus"
    assert b.address == "abc-123"
    assert b.model == "JPCP"
    assert b.name == "JPCP-dataplane-bus"


def test_yaml_streams_legacy_shim_skipped_if_already_declared(monkeypatch):
    monkeypatch.setenv(_LEGACY_ENV, "1")
    model_yaml = {
        "name": "JPCP",
        "dataplane_bus_uuid": "abc-123",
        "inference": {"streams": [{"name": "sb", "connector": "dataplane-bus", "address": "explicit"}]},
    }
    result = bindings.yaml_streams(model_yaml, project="")
    assert len(result) == 1
    assert result[0].address == "explicit"


# ── tenancy (ruling R11) + ASCII validation (fix round 1 #3) ────────────────


def test_define_stream_global_project_ok_for_unscoped_model(pack):
    _write_model(pack, "UNSCOPED-MODEL")
    b = StreamBinding(
        project="_global",
        name="s",
        connector="http",
        model="UNSCOPED-MODEL",
        alias="Production",
        address="",
        connection=None,
    )
    stored = bindings.define_stream(b, actor="alice")
    assert stored.project == ""
    assert stored.origin == "api"
    assert bindings.get_binding("s", "") is not None


def test_define_stream_global_project_refused_for_scoped_model(pack):
    _write_model(pack, "SCOPED-MODEL")
    create_project("research")
    _assign_model("research", "SCOPED-MODEL")
    b = StreamBinding(
        project="_global",
        name="s",
        connector="http",
        model="SCOPED-MODEL",
        alias="Production",
        address="",
        connection=None,
    )
    with pytest.raises(SpecError) as exc:
        bindings.define_stream(b, actor="alice")
    msg = str(exc.value)
    assert "SCOPED-MODEL" in msg
    assert "_global" in msg
    assert "research" not in msg  # never reveal which other project owns it


def test_define_stream_matching_project_ok(pack):
    _write_model(pack, "MY-MODEL")
    create_project("research")
    _assign_model("research", "MY-MODEL")
    b = StreamBinding(
        project="research",
        name="s",
        connector="http",
        model="MY-MODEL",
        alias="Production",
        address="",
        connection=None,
    )
    stored = bindings.define_stream(b, actor="alice")
    assert stored.project == "research"


def test_define_stream_other_project_refused_without_leaking_owner(pack):
    _write_model(pack, "OWNED-MODEL")
    create_project("research")
    create_project("other")
    _assign_model("research", "OWNED-MODEL")
    b = StreamBinding(
        project="other",
        name="s",
        connector="http",
        model="OWNED-MODEL",
        alias="Production",
        address="",
        connection=None,
    )
    with pytest.raises(SpecError) as exc:
        bindings.define_stream(b, actor="alice")
    msg = str(exc.value)
    assert "OWNED-MODEL" in msg
    assert "other" in msg
    assert "research" not in msg  # never reveal the owning project


def test_define_stream_case_insensitive_model_match_stores_canonical_spelling(pack):
    """Fix round 1, finding 4: the pack's own spelling is stored, never the caller's."""
    _write_model(pack, "JPCP")
    create_project("research")
    _assign_model("research", "JPCP")
    b = StreamBinding(
        project="research",
        name="s",
        connector="http",
        model="jpcp",  # MLflow-cased spelling
        alias="Production",
        address="",
        connection=None,
    )
    stored = bindings.define_stream(b, actor="alice")
    assert stored.model == "JPCP"  # canonicalised, not the caller's "jpcp"


def test_define_stream_unknown_model_raises(pack):
    b = StreamBinding(
        project="",
        name="s",
        connector="http",
        model="NO-SUCH-MODEL",
        alias="Production",
        address="",
        connection=None,
    )
    with pytest.raises(SpecError, match="unknown model"):
        bindings.define_stream(b, actor="alice")


def test_define_stream_refuses_an_unknown_connector_kind(pack):
    """M5: a typo used to be stored happily, and the supervisor then parked the stream in
    ``error`` for ever with nothing said at definition time."""
    _write_model(pack, "UNSCOPED")
    b = StreamBinding(
        project="",
        name="s",
        connector="kafak",
        model="UNSCOPED",
        alias="Production",
        address="",
        connection=None,
    )
    with pytest.raises(SpecError, match="unknown stream connector 'kafak'"):
        bindings.define_stream(b, actor="alice")
    assert bindings.get_binding("s", "") is None  # nothing was written


@pytest.mark.parametrize("kind", ["http", "kafka"])
def test_define_stream_accepts_a_registered_connector_and_the_push_kind(pack, kind):
    _write_model(pack, "UNSCOPED")
    b = StreamBinding(
        project="",
        name=f"s-{kind}",
        connector=kind,
        model="UNSCOPED",
        alias="Production",
        address="topic-x",
        connection=None,
    )
    assert bindings.define_stream(b, actor="alice").connector == kind


def test_a_pack_entry_may_name_a_connector_the_core_does_not_register(pack):
    """The pack's own connectors (Dataplane bus arrives as pack content) must still sync — only the
    API/CLI path is held to the registry."""
    (pack / "packmodel.yaml").write_text(
        "name: PACKMODEL\ninference:\n  streams:\n    - name: bus1\n      connector: dataplane-bus\n"
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["errors"] == [] and report["synced"] == ["_global/bus1"]
    assert bindings.get_binding("bus1", "").connector == "dataplane-bus"


def test_yaml_streams_labels_pack_bindings_pack(pack):
    """M12: the pure parser exists to parse *pack* content, and used to return origin='api' for
    exactly those bindings. Nothing stored ever changed (the write path passes its own origin),
    but the returned value was a lie."""
    model_yaml = {
        "name": "M1",
        "inference": {"streams": [{"name": "s1", "connector": "http"}]},
    }
    (binding,) = bindings.yaml_streams(model_yaml, project="")
    assert binding.origin == "pack"


def test_define_stream_writes_audit_event(pack):
    _write_model(pack, "UNSCOPED")
    b = StreamBinding(
        project="",
        name="audited",
        connector="http",
        model="UNSCOPED",
        alias="Production",
        address="",
        connection=None,
    )
    bindings.define_stream(b, actor="alice")
    events = [e for e in export_audit_events() if e["action"] == "dataplane_stream_defined"]
    assert len(events) == 1
    assert events[0]["actor"] == "alice"
    assert events[0]["target"] == "_global/audited"


@pytest.mark.parametrize("bad_model", [f"MACK{_KELVIN}", f"{_FULLWIDTH_J}PCP"])
def test_define_stream_rejects_non_ascii_model(pack, bad_model):
    """Fix round 1, finding 3 — the Kelvin sign (U+212A) folds to 'k' in Python's str.lower() but
    not in SQLite's lower(); a fullwidth letter is a different non-ASCII case entirely. Both must
    be rejected before any tenancy lookup."""
    b = StreamBinding(
        project="_global",
        name="s",
        connector="http",
        model=bad_model,
        alias="Production",
        address="",
        connection=None,
    )
    with pytest.raises(SpecError):
        bindings.define_stream(b, actor="alice")


def test_define_stream_rejects_non_ascii_stream_name(pack):
    _write_model(pack, "SOME-MODEL")
    b = StreamBinding(
        project="",
        name=f"s{_KELVIN}",
        connector="http",
        model="SOME-MODEL",
        alias="Production",
        address="",
        connection=None,
    )
    with pytest.raises(SpecError):
        bindings.define_stream(b, actor="alice")


def test_define_stream_rejects_non_ascii_project(pack):
    _write_model(pack, "SOME-MODEL2")
    b = StreamBinding(
        project=f"proj{_KELVIN}",
        name="s",
        connector="http",
        model="SOME-MODEL2",
        alias="Production",
        address="",
        connection=None,
    )
    with pytest.raises(SpecError):
        bindings.define_stream(b, actor="alice")


# ── origin ownership (fix round 1, finding 1) ───────────────────────────────


def test_define_stream_refuses_to_overwrite_pack_row(pack):
    _write_model(
        pack,
        "COLL-MODEL",
        inference={"streams": [{"name": "s", "connector": "http", "address": "pack-addr"}]},
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/s"]

    b = StreamBinding(
        project="",
        name="s",
        connector="kafka",
        model="COLL-MODEL",
        alias="Production",
        address="api-addr",
        connection=None,
    )
    with pytest.raises(SpecError, match="use-case pack"):
        bindings.define_stream(b, actor="alice")
    row = catalog.get_stream("s", "")
    assert row["origin"] == "pack"
    assert row["address"] == "pack-addr"  # untouched


def test_sync_pack_streams_skips_api_owned_collision(pack):
    _write_model(pack, "COLL-MODEL2")
    b = StreamBinding(
        project="",
        name="s",
        connector="http",
        model="COLL-MODEL2",
        alias="Production",
        address="api-addr",
        connection=None,
    )
    bindings.define_stream(b, actor="alice")

    _write_model(
        pack,
        "COLL-MODEL2",
        inference={"streams": [{"name": "s", "connector": "kafka", "address": "pack-addr"}]},
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == []
    assert report["conflicts"] == ["_global/s"]
    row = catalog.get_stream("s", "")
    assert row["origin"] == "api"
    assert row["address"] == "api-addr"  # untouched by the colliding pack entry


# ── pack sync: idempotency, sweep safety (fix round 1, finding 2) ───────────


def test_sync_pack_streams_idempotent_and_keeps_state(pack):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    report1 = bindings.sync_pack_streams(actor="ci")
    assert report1["synced"] == ["_global/http-in"]
    row = catalog.get_stream("http-in", "")
    assert row["origin"] == "pack"
    assert row["state"] == "enabled"

    # An operator pauses it by hand.
    catalog.set_stream_state("", "http-in", "paused", reason="operator_note")

    # Re-sync with the SAME pack: idempotent, state must not be clobbered back to enabled.
    report2 = bindings.sync_pack_streams(actor="ci")
    assert report2["synced"] == ["_global/http-in"]
    assert report2["disabled"] == []
    row = catalog.get_stream("http-in", "")
    assert row["state"] == "paused"
    assert row["state_reason"] == "operator_note"


def test_sync_pack_streams_disables_removed_entry_and_audits(pack):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    bindings.sync_pack_streams(actor="ci")
    assert catalog.get_stream("http-in", "")["state"] == "enabled"

    # The model YAML no longer declares this stream.
    _write_model(pack, "JPCP", inference={"streams": []})
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == []
    assert report["disabled"] == ["_global/http-in"]
    row = catalog.get_stream("http-in", "")
    assert row is not None  # never deleted
    assert row["state"] == "disabled"
    assert row["state_reason"] == "removed_from_pack"

    events = [e for e in export_audit_events() if e["action"] == "dataplane_stream_disabled"]
    assert len(events) == 1
    assert events[0]["target"] == "_global/http-in"
    assert json.loads(events[0]["details"])["reason"] == "removed_from_pack"

    # A further sync with the entry still gone is a no-op (already disabled).
    report2 = bindings.sync_pack_streams(actor="ci")
    assert report2["disabled"] == []


def test_a_sweep_audit_loss_is_counted_and_the_disable_still_stands(pack, monkeypatch):
    """An audit outage must not un-do the sweep, and must not hide itself either (P8.77).

    ``sync_pack_streams`` writes its disable through ``audit_best_effort``: the stream is disabled
    whether or not the record lands, so a blinking audit datastore cannot leave a removed stream
    live. The loss is then counted, because the hash chain proves integrity over the rows that
    exist and can never show a row that was never appended. The counter is process-global, so this
    test resets it on both sides; without that the count leaks between tests under ``-n auto``.
    """
    from examlops.data import audit as audit_mod

    audit_mod.reset_dropped_audit_events()
    try:
        _write_model(
            pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]}
        )
        bindings.sync_pack_streams(actor="ci")

        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("audit datastore unavailable")

        monkeypatch.setattr(audit_mod, "write_audit_event", boom)
        _write_model(pack, "JPCP", inference={"streams": []})
        report = bindings.sync_pack_streams(actor="ci")

        # The operation the operator asked for survived the audit outage.
        assert report["disabled"] == ["_global/http-in"]
        row = catalog.get_stream("http-in", "")
        assert row["state"] == "disabled"
        assert row["state_reason"] == "removed_from_pack"
        # And the platform can tell that a record is missing for this window.
        assert audit_mod.dropped_audit_events().get("dataplane_stream_disabled") == 1
    finally:
        audit_mod.reset_dropped_audit_events()


def test_sync_pack_streams_readded_entry_is_reenabled_and_audited(pack):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    bindings.sync_pack_streams(actor="ci")
    _write_model(pack, "JPCP", inference={"streams": []})
    bindings.sync_pack_streams(actor="ci")
    assert catalog.get_stream("http-in", "")["state"] == "disabled"

    # The entry comes back.
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/http-in"]
    assert report["enabled"] == ["_global/http-in"]
    row = catalog.get_stream("http-in", "")
    assert row["state"] == "enabled"
    assert row["state_reason"] is None

    events = [e for e in export_audit_events() if e["action"] == "dataplane_stream_enabled"]
    assert len(events) == 1
    assert events[0]["target"] == "_global/http-in"
    assert json.loads(events[0]["details"])["reason"] == "readded_to_pack"


def test_sync_pack_streams_human_disabled_stream_stays_disabled(pack):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    bindings.sync_pack_streams(actor="ci")

    # A human disables it directly (not via the sweep) — no reason, or any reason other than the
    # sweep's own sentinel.
    catalog.set_stream_state("", "http-in", "disabled", reason="operator_shutoff")

    # The entry is (still) present in the pack; a sync must not touch a human's decision.
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/http-in"]
    assert report["enabled"] == []  # NOT re-enabled — this wasn't the sweep's own disable
    row = catalog.get_stream("http-in", "")
    assert row["state"] == "disabled"
    assert row["state_reason"] == "operator_shutoff"

    enabled_events = [e for e in export_audit_events() if e["action"] == "dataplane_stream_enabled"]
    assert enabled_events == []


# ── fix round 2, N1: a human pause must survive a removal-and-re-add ────────


def test_sync_pack_streams_paused_survives_removal_and_readd(pack):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    bindings.sync_pack_streams(actor="ci")

    # An operator pauses it — not disables it.
    catalog.set_stream_state("", "http-in", "paused", reason="operator_note")

    # The entry disappears from the pack.
    _write_model(pack, "JPCP", inference={"streams": []})
    report = bindings.sync_pack_streams(actor="ci")
    assert report["disabled"] == ["_global/http-in"]
    row = catalog.get_stream("http-in", "")
    assert row["state"] == "disabled"
    assert row["state_reason"] == "removed_from_pack:paused"

    disabled_events = [
        e for e in export_audit_events() if e["action"] == "dataplane_stream_disabled"
    ]
    assert len(disabled_events) == 1
    disabled_details = json.loads(disabled_events[0]["details"])
    assert disabled_details["reason"] == "removed_from_pack:paused"
    assert disabled_details["prior_state"] == "paused"

    # The entry comes back — the stream must return to PAUSED, not enabled.
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    report2 = bindings.sync_pack_streams(actor="ci")
    assert report2["enabled"] == ["_global/http-in"]  # "enabled" = "the sweep restored it" here
    row = catalog.get_stream("http-in", "")
    assert row["state"] == "paused"  # restored, NOT "enabled"
    assert row["state_reason"] is None  # the sweep's own reason is cleared

    restore_events = [e for e in export_audit_events() if e["action"] == "dataplane_stream_enabled"]
    assert len(restore_events) == 1
    restore_details = json.loads(restore_events[0]["details"])
    assert restore_details["reason"] == "readded_to_pack"
    assert restore_details["restored_state"] == "paused"


def test_sync_pack_streams_human_disabled_row_untouched_through_removal_and_readd(pack):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    bindings.sync_pack_streams(actor="ci")
    catalog.set_stream_state("", "http-in", "disabled", reason="operator_shutoff")

    # Removed from the pack — a human's disable must not be touched, audited, or re-labelled.
    _write_model(pack, "JPCP", inference={"streams": []})
    report = bindings.sync_pack_streams(actor="ci")
    assert report["disabled"] == []
    row = catalog.get_stream("http-in", "")
    assert row["state"] == "disabled"
    assert row["state_reason"] == "operator_shutoff"

    # Re-added — still not the sweep's own reason, so it must stay disabled exactly as the human
    # left it.
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    report2 = bindings.sync_pack_streams(actor="ci")
    assert report2["enabled"] == []
    row = catalog.get_stream("http-in", "")
    assert row["state"] == "disabled"
    assert row["state_reason"] == "operator_shutoff"

    assert [e for e in export_audit_events() if e["action"] == "dataplane_stream_disabled"] == []
    assert [e for e in export_audit_events() if e["action"] == "dataplane_stream_enabled"] == []


def test_sync_pack_streams_no_sweep_on_parse_error(pack):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    bindings.sync_pack_streams(actor="ci")
    assert catalog.get_stream("http-in", "")["state"] == "enabled"

    # JPCP's own entry disappears...
    _write_model(pack, "JPCP", inference={"streams": []})
    # ...and an unrelated sibling file is unparsable YAML.
    (pack / "broken.yaml").write_text("name: [unterminated")

    report = bindings.sync_pack_streams(actor="ci")
    assert report["errors"], "the broken sibling file must be recorded as an error"
    assert report["disabled"] == []  # the sweep must NOT have run
    assert catalog.get_stream("http-in", "")["state"] == "enabled"  # untouched


def test_sync_pack_streams_no_sweep_on_missing_or_empty_dir(pack, tmp_path, monkeypatch):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    bindings.sync_pack_streams(actor="ci")
    assert catalog.get_stream("http-in", "")["state"] == "enabled"

    # Point at a directory that was never created.
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(tmp_path / "does-not-exist"))
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == []
    assert report["disabled"] == []
    assert catalog.get_stream("http-in", "")["state"] == "enabled"  # untouched

    # Point at a directory that exists but holds no model YAMLs.
    empty_models = tmp_path / "empty_pack" / "models"
    empty_models.mkdir(parents=True)
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(tmp_path / "empty_pack"))
    report2 = bindings.sync_pack_streams(actor="ci")
    assert report2["synced"] == []
    assert report2["disabled"] == []
    assert catalog.get_stream("http-in", "")["state"] == "enabled"  # untouched


def test_sync_pack_streams_never_touches_api_origin_stream(pack):
    catalog.upsert_stream(
        "",
        "http-in",
        connector="http",
        model="OTHER-MODEL",
        alias="Production",
        address="",
        connection=None,
        options={},
        limits={},
        state="enabled",
        origin="api",
        actor="alice",
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["disabled"] == []  # api-origin row untouched even though pack has no entry
    assert catalog.get_stream("http-in", "")["state"] == "enabled"
    assert catalog.get_stream("http-in", "")["origin"] == "api"


def test_sync_pack_streams_enforces_tenancy(pack):
    """A YAML that names a project its model does not belong to is refused (R11), and the refusal
    never says which project does own it."""
    create_project("research")
    create_project("other")
    _assign_model("research", "JPCP")
    _write_model(
        pack,
        "JPCP",
        project="other",
        inference={"streams": [{"name": "http-in", "connector": "http"}]},
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == []
    assert report["errors"] and "JPCP" in report["errors"][0]["message"]
    assert "research" not in report["errors"][0]["message"]
    assert catalog.get_stream("http-in", "other") is None


# ── the project a pack stream lands in (live finding D6) ────────────────────


def test_a_pack_model_with_no_project_key_takes_the_project_it_is_assigned_to(pack):
    """Live finding D6: on a real site no pack model carries a top-level ``project:`` while every
    model IS assigned to a project, so R11 refused every pack-declared stream — the only way to
    define one in this release — and the refusal was recorded but never logged."""
    create_project("research")
    _assign_model("research", "JPCP")
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    report = bindings.sync_pack_streams(actor="ci")
    assert report["errors"] == [] and report["synced"] == ["research/http-in"]
    row = catalog.get_stream("http-in", "research")
    assert row is not None and row["model"] == "JPCP" and row["origin"] == "pack"
    assert catalog.get_stream("http-in", "") is None  # not the unscoped default


def test_a_pack_model_assigned_to_no_project_stays_unscoped(pack):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    report = bindings.sync_pack_streams(actor="ci")
    assert report["errors"] == [] and report["synced"] == ["_global/http-in"]
    assert catalog.get_stream("http-in", "")["project"] == ""


def test_a_pack_model_in_several_projects_is_an_error_naming_the_model_only(pack):
    """More than one candidate is not a guess to make: the operator says which, in the YAML. The
    message names the model and never the projects (R11's no-leak rule)."""
    for project in ("research", "ops"):
        create_project(project)
        _assign_model(project, "JPCP")
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == []
    message = report["errors"][0]["message"]
    assert "JPCP" in message and "project:" in message
    assert "research" not in message and "ops" not in message
    assert catalog.get_stream("http-in", "research") is None

    # …and naming one in the YAML resolves it.
    _write_model(
        pack,
        "JPCP",
        project="ops",
        inference={"streams": [{"name": "http-in", "connector": "http"}]},
    )
    assert bindings.sync_pack_streams(actor="ci")["synced"] == ["ops/http-in"]


def test_a_stream_less_model_in_several_projects_is_not_an_error(pack, caplog):
    """Re-review regression: the project was resolved for every model YAML, before its
    ``inference.streams`` was read. A model assigned to several projects — a supported
    configuration, ``project_models``' PK is ``(project, model)`` — therefore errored even when it
    declared no stream at all, and any error suppresses the pack-removal sweep for the WHOLE pack,
    on every sync. That is the failure D6 was raised about, reached from a YAML declaring nothing.
    """
    create_project("research")
    _assign_model("research", "JPCP")
    for project in ("p1", "p2"):
        create_project(project)
        _assign_model(project, "OTHER")
    _write_model(pack, "JPCP", inference={"streams": [{"name": "http-in", "connector": "http"}]})
    _write_model(pack, "OTHER", inference={"streams": []})  # declares nothing at all

    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.bindings"):
        report = bindings.sync_pack_streams(actor="ci")
    assert report["errors"] == [] and report["synced"] == ["research/http-in"]
    assert "sweep did not run" not in caplog.text

    # …and the sweep still works: remove the entry and it is disabled, as it could not be while
    # the stream-less model kept reporting an error.
    _write_model(pack, "JPCP", inference={"streams": []})
    report2 = bindings.sync_pack_streams(actor="ci")
    assert report2["errors"] == [] and report2["disabled"] == ["research/http-in"]
    assert catalog.get_stream("http-in", "research")["state"] == "disabled"


def test_sync_errors_are_logged_and_a_suppressed_sweep_says_so(pack, caplog):
    """Live finding D6: ``report["errors"]`` was the only record of a refused entry and nothing
    read it, so an operator whose stream never appeared had an empty log to go on."""
    _write_model(
        pack,
        "JPCP",
        inference={
            "streams": [
                {"name": "good", "connector": "http"},
                {"name": "bad-state", "connector": "http", "state": "Enabled"},
            ]
        },
    )
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.bindings"):
        report = bindings.sync_pack_streams(actor="ci")
    assert len(report["errors"]) == 1
    assert "jpcp.yaml entry 1 refused" in caplog.text
    assert "invalid state 'Enabled'" in caplog.text
    assert "the pack-removal sweep did not run (1 entry error(s))" in caplog.text


def test_a_clean_sync_says_nothing_about_a_suppressed_sweep(pack, caplog):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "good", "connector": "http"}]})
    with caplog.at_level(logging.INFO, logger="examlops.dataplane.streams.bindings"):
        assert bindings.sync_pack_streams(actor="ci")["errors"] == []
    assert "sweep did not run" not in caplog.text


# ── per-entry robustness (fix round 1, finding 5) ───────────────────────────


def test_sync_pack_streams_isolates_a_bad_entry_from_a_good_one(pack):
    _write_model(
        pack,
        "JPCP",
        inference={
            "streams": [
                {"name": "good", "connector": "http"},
                {"name": "bad-state", "connector": "http", "state": "Enabled"},  # invalid
            ]
        },
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/good"]
    assert catalog.get_stream("good", "")["state"] == "enabled"
    assert len(report["errors"]) == 1
    assert report["errors"][0]["file"] == "jpcp.yaml"
    assert report["errors"][0]["entry"] == 1
    assert catalog.get_stream("bad-state", "") is None


def test_sync_pack_streams_isolates_a_non_dict_entry(pack):
    _write_model(
        pack,
        "JPCP",
        inference={"streams": [{"name": "good", "connector": "http"}, "not-a-dict"]},
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/good"]
    assert len(report["errors"]) == 1
    assert report["errors"][0]["entry"] == 1


def test_sync_pack_streams_isolates_a_broken_file_from_a_good_one(pack):
    _write_model(pack, "JPCP", inference={"streams": [{"name": "good", "connector": "http"}]})
    (pack / "broken.yaml").write_text("name: [unterminated")
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/good"]
    assert any(e["file"] == "broken.yaml" for e in report["errors"])


def test_sync_pack_streams_error_message_has_no_yaml_snippet(pack):
    (pack / "broken.yaml").write_text('name: "unterminated string value')
    report = bindings.sync_pack_streams(actor="ci")
    assert report["errors"]
    msg = report["errors"][0]["message"]
    assert "unterminated string value" not in msg


# ── fix round 2, I5: four re-review reproductions, each isolated + sweep-suppressed ─────────


def test_sync_pack_streams_isolates_non_str_project(pack):
    """`project: 123` used to raise an uncaught AttributeError in `_normalize_project`.

    The bad model declares a stream: since the re-review, a model's project is resolved only when
    it declares one, so a malformed `project:` on a model that declares nothing is never read (and
    must not cost the pack its removal sweep) — see
    `test_a_stream_less_model_in_several_projects_is_not_an_error`.
    """
    _write_model(
        pack,
        "BADPROJECT",
        project=123,
        inference={"streams": [{"name": "bad", "connector": "http"}]},
    )
    _write_model(pack, "GOOD", inference={"streams": [{"name": "good", "connector": "http"}]})
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/good"]
    assert any(e["file"] == "badproject.yaml" for e in report["errors"])
    assert catalog.get_stream("good", "")["state"] == "enabled"
    assert report["disabled"] == []  # any error suppresses the sweep


def test_sync_pack_streams_isolates_unserializable_options(pack):
    """An unquoted YAML date inside `options` parses to `datetime.date`, which `json.dumps`
    cannot encode — it used to raise a TypeError deep inside `upsert_stream`."""
    _write_model(
        pack,
        "BADOPTIONS",
        inference={
            "streams": [
                {
                    "name": "bad",
                    "connector": "http",
                    "options": {"since": datetime.date(2024, 1, 1)},
                },
                {"name": "good", "connector": "http"},
            ]
        },
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/good"]
    assert len(report["errors"]) == 1
    assert report["errors"][0]["entry"] == 0
    assert catalog.get_stream("bad", "") is None
    assert catalog.get_stream("good", "")["state"] == "enabled"


def test_sync_pack_streams_isolates_non_str_address(pack):
    """`address: {x: 1}` used to raise sqlite3.ProgrammingError from inside the upsert, outside
    the per-entry try."""
    _write_model(
        pack,
        "BADADDRESS",
        inference={
            "streams": [
                {"name": "bad", "connector": "http", "address": {"x": 1}},
                {"name": "good", "connector": "http"},
            ]
        },
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/good"]
    assert len(report["errors"]) == 1
    assert report["errors"][0]["entry"] == 0
    assert catalog.get_stream("bad", "") is None
    assert catalog.get_stream("good", "")["state"] == "enabled"


def test_sync_pack_streams_isolates_non_str_connection(pack):
    """`connection: [a, b]` used to raise sqlite3.ProgrammingError from inside the upsert, outside
    the per-entry try."""
    _write_model(
        pack,
        "BADCONNECTION",
        inference={
            "streams": [
                {"name": "bad", "connector": "http", "connection": ["a", "b"]},
                {"name": "good", "connector": "http"},
            ]
        },
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["synced"] == ["_global/good"]
    assert len(report["errors"]) == 1
    assert report["errors"][0]["entry"] == 0
    assert catalog.get_stream("bad", "") is None
    assert catalog.get_stream("good", "")["state"] == "enabled"


def test_sync_pack_streams_isolated_entry_never_leaks_values_in_error_message(pack):
    """Whatever triggers the last-resort `except Exception` net, the recorded message must never
    contain the entry's own values — only the exception type and its position."""
    _write_model(
        pack,
        "BADCONNECTION2",
        inference={
            "streams": [{"name": "bad", "connector": "http", "connection": ["super-secret-value"]}]
        },
    )
    report = bindings.sync_pack_streams(actor="ci")
    assert report["errors"]
    msg = report["errors"][0]["message"]
    assert "super-secret-value" not in msg


# ── parity: bindings.yaml_streams vs pipelines.model_loader.normalize_inference_streams ────


_PARITY_RAW = {
    "name": "JPCP",
    "config_class": "jpcp_config.JPCPConfiguration",
    "task_type": "regression",
    "dataplane_bus_uuid": "legacy-uuid-1",
    "inference": {
        "streams": [
            {"name": "http-in", "connector": "http", "address": "https://x/infer"},
            {
                "name": "kafka-in",
                "connector": "kafka",
                "address": "topic-a",
                "alias": "Canary",
                "connection": "kconn",
                "options": {"passthrough": ["a"]},
                "limits": {"max_in_flight": 16, "unknown_future_key": 999},
            },
        ]
    },
}


def _binding_as_dict(b: StreamBinding) -> dict:
    from dataclasses import asdict

    return {
        "name": b.name,
        "connector": b.connector,
        "model": b.model,
        "alias": b.alias,
        "address": b.address,
        "connection": b.connection,
        "options": b.options,
        "limits": asdict(b.limits),
    }


@pytest.mark.parametrize("legacy_env", [None, "1"])
def test_yaml_streams_and_normalizer_agree(monkeypatch, legacy_env):
    if legacy_env is None:
        monkeypatch.delenv(_LEGACY_ENV, raising=False)
    else:
        monkeypatch.setenv(_LEGACY_ENV, legacy_env)

    via_bindings = [_binding_as_dict(b) for b in bindings.yaml_streams(_PARITY_RAW, project="")]

    cfg = ModelYAMLConfig(
        name=_PARITY_RAW["name"],
        config_class=_PARITY_RAW["config_class"],
        task_type=_PARITY_RAW["task_type"],
        dataplane_bus_uuid=_PARITY_RAW["dataplane_bus_uuid"],
        inference=_PARITY_RAW["inference"],
    )
    via_normalizer = normalize_inference_streams(cfg)

    assert via_bindings == via_normalizer


# ── the column migration itself (live finding D1) ───────────────────────────


def test_a_stream_table_created_before_state_reason_gains_it_on_the_next_init_db():
    """Live finding D1, reproduced: ``state_reason`` was added to the ``dataplane_streams`` DDL
    after the table already existed on a running instance. ``init_db`` only creates *missing*
    tables, so the column never arrived and every state change — pause, resume, disable, and the
    pack-removal sweep — failed with ``no such column: state_reason``: a 500 with a traceback, on
    the only route that can pause a live stream.

    The old shape is built by dropping the column from a current database rather than by copying
    the DDL, so this test cannot drift from the schema it is about, and it runs on whichever
    datastore the suite is configured for.
    """
    from examlops.data import get_db, init_db

    init_db()
    catalog.upsert_stream(
        "",
        "pre-migration",
        connector="http",
        model="JPCP",
        alias="Production",
        address="",
        connection=None,
        options={},
        limits={},
        state="enabled",
        origin="api",
        actor="ci",
    )
    with get_db() as conn:  # the pre-release shape, with a row already in it
        conn.execute("ALTER TABLE dataplane_streams DROP COLUMN state_reason")

    with pytest.raises(Exception, match="state_reason"):
        catalog.set_stream_state("", "pre-migration", "paused")  # what 500'd live

    init_db(force=True)  # the schema bootstrap an upgraded process runs

    assert catalog.set_stream_state("", "pre-migration", "paused", reason="ops") is True
    row = catalog.get_stream("pre-migration", "")
    assert row["state"] == "paused" and row["state_reason"] == "ops"
