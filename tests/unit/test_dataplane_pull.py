"""ADR 0130 — source definitions and the pull orchestrator (fake connector, file:// store)."""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import dataplane as dpl  # noqa: E402
from examlops.connections import create_connection  # noqa: E402
from examlops.data import dataplane as catalog  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.dataplane.connectors import registry  # noqa: E402
from examlops.dataplane.connectors.base import BaseConnector  # noqa: E402
from examlops.dataplane.types import (  # noqa: E402
    DataplaneError,
    LimitExceeded,
    Limits,
    Probe,
    PullInProgress,
    SpecError,
    TableBatch,
)


class _Counting(BaseConnector):
    """Emits rows 1..n, or only rows after the watermark when incremental."""

    kind = "counting"
    connection_kinds = ()
    connection_required = False
    supports_incremental = True
    n = 3

    def probe(self, conn, spec=None):
        return Probe(True, "ok")

    def read(self, conn, spec, since, limits):
        start = (since or {}).get("value", 0)
        rows = [{"id": i} for i in range(start + 1, self.n + 1)]
        yield TableBatch(
            "rows",
            pa.RecordBatch.from_pylist(rows, schema=pa.schema([("id", pa.int64())])),
            {"column": "id", "value": self.n, "type": "int"},
        )


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    registry.reset()
    registry.register(_Counting())
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    # deterministic Fernet key so the D7 secrets client works offline (needed by the
    # secret-redaction tests below, which resolve a real Named Connection secret).
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", "3jZ8n4bQ6h3nJh5m3nJh5m3nJh5m3nJh5m3nJh5m3nI=")
    yield
    registry.reset()


def test_define_rejects_secret_looking_keys_and_bad_specs():
    with pytest.raises(SpecError, match="password"):
        dpl.define_source("s", "counting", spec={"password": "x"})
    with pytest.raises(SpecError, match="unknown connector"):
        dpl.define_source("s", "nope")


def test_pull_commits_indexes_and_audits(tmp_path):
    dpl.define_source("s", "counting", spec={}, actor="t")
    result = dpl.run_pull("s", actor="t")
    assert result.status == "succeeded" and result.row_count == 3
    assert catalog.last_pull("", "s")["revision"] == result.revision
    actions = {e["action"] for e in export_audit_events()}
    assert {"dataplane_source_created", "dataplane_pull_succeeded"} <= actions


def test_second_identical_pull_is_unchanged():
    dpl.define_source("s", "counting", spec={})
    first = dpl.run_pull("s")
    second = dpl.run_pull("s")
    assert second.status == "unchanged" and second.revision == first.revision


def test_incremental_pull_adds_only_new_rows():
    dpl.define_source("s", "counting", spec={"incremental": True})
    dpl.run_pull("s")
    _Counting.n = 5
    try:
        result = dpl.run_pull("s")
    finally:
        _Counting.n = 3
    assert result.row_count == 5  # 3 carried + 2 new


def test_limit_failure_marks_the_pull_failed_and_keeps_latest():
    dpl.define_source("s", "counting", spec={})
    ok = dpl.run_pull("s")
    dpl.define_source("s", "counting", spec={}, limits=Limits(max_rows=1))
    with pytest.raises(LimitExceeded):
        dpl.run_pull("s")
    assert catalog.last_pull("", "s", committed_only=False)["status"] == "failed"
    assert dpl.resolve(dpl.store_from_env(), dpl.source_key("", "s")).revision == ok.revision


def test_concurrent_pull_is_refused(monkeypatch):
    dpl.define_source("s", "counting", spec={})

    class _Busy:
        def try_lock(self, *a, **k):
            return False

        def unlock(self, *a, **k):
            pass

    monkeypatch.setattr("examlops.dataplane.pull.get_coordinator", lambda: _Busy())
    with pytest.raises(PullInProgress):
        dpl.run_pull("s")


def test_preview_reads_without_committing():
    dpl.define_source("s", "counting", spec={})
    assert dpl.preview("s", limit=2) == [{"id": 1}, {"id": 2}]
    assert catalog.last_pull("", "s") is None


# ── fix round 1: transient retry + secret redaction on the preview/probe paths ──────────


def test_transient_read_error_retries_and_does_not_duplicate_rows():
    calls = {"n": 0}

    class _FlakyOnce(BaseConnector):
        kind = "flaky_once"
        connection_kinds = ()
        connection_required = False
        supports_incremental = False

        def probe(self, conn, spec=None):
            return Probe(True, "ok")

        def read(self, conn, spec, since, limits):
            calls["n"] += 1
            if calls["n"] == 1:
                # Writes one row, then drops mid-stream: proves a partial attempt is
                # discarded wholesale rather than merged with the retried one.
                yield TableBatch(
                    "rows",
                    pa.RecordBatch.from_pylist([{"id": 1}], schema=pa.schema([("id", pa.int64())])),
                )
                raise ConnectionError("dropped mid-stream")
            rows = [{"id": 1}, {"id": 2}]
            yield TableBatch(
                "rows", pa.RecordBatch.from_pylist(rows, schema=pa.schema([("id", pa.int64())]))
            )

    registry.register(_FlakyOnce())
    dpl.define_source("flaky", "flaky_once", spec={})
    result = dpl.run_pull("flaky")
    assert result.status == "succeeded"
    assert result.row_count == 2  # not 3 - the aborted first attempt's row was not carried over
    assert calls["n"] == 2


def test_non_transient_read_error_is_not_retried():
    calls = {"n": 0}

    class _SpecErrorOnRead(BaseConnector):
        kind = "spec_error_on_read"
        connection_kinds = ()
        connection_required = False
        supports_incremental = False

        def probe(self, conn, spec=None):
            return Probe(True, "ok")

        def read(self, conn, spec, since, limits):
            calls["n"] += 1
            raise SpecError("bad read")

    registry.register(_SpecErrorOnRead())
    dpl.define_source("bad", "spec_error_on_read", spec={})
    with pytest.raises(SpecError):
        dpl.run_pull("bad")
    assert calls["n"] == 1


def test_persistent_transient_error_fails_after_retries_exhausted():
    calls = {"n": 0}

    class _AlwaysConnFail(BaseConnector):
        kind = "always_fail"
        connection_kinds = ()
        connection_required = False
        supports_incremental = False

        def probe(self, conn, spec=None):
            return Probe(True, "ok")

        def read(self, conn, spec, since, limits):
            calls["n"] += 1
            raise ConnectionError("still down")

    registry.register(_AlwaysConnFail())
    dpl.define_source("downed", "always_fail", spec={})
    with pytest.raises(DataplaneError):
        dpl.run_pull("downed")
    assert calls["n"] == 3  # 1 initial attempt + 2 retries
    assert catalog.last_pull("", "downed", committed_only=False)["status"] == "failed"


class _SecretLeaker(BaseConnector):
    """Raises with the resolved connection secret embedded in the message."""

    connection_kinds = ()
    connection_required = False
    supports_incremental = False

    def probe(self, conn, spec=None):
        raise RuntimeError(f"probe failed secret={(conn or {}).get('secret')}")

    def read(self, conn, spec, since, limits):
        raise RuntimeError(f"read failed secret={(conn or {}).get('secret')}")


def test_preview_redacts_secret_on_connector_error():
    leaker = _SecretLeaker()
    leaker.kind = "secret_leaker_preview"
    registry.register(leaker)
    create_connection("c1", "uri", config={}, secret_value="sekret-value")
    dpl.define_source("leaky", "secret_leaker_preview", spec={}, connection="c1")
    with pytest.raises(DataplaneError) as ei:
        dpl.preview("leaky")
    assert "sekret-value" not in str(ei.value)


def test_probe_source_redacts_secret_on_connector_error():
    leaker = _SecretLeaker()
    leaker.kind = "secret_leaker_probe"
    registry.register(leaker)
    create_connection("c2", "uri", config={}, secret_value="sekret-value-2")
    dpl.define_source("leaky2", "secret_leaker_probe", spec={}, connection="c2")
    probe = dpl.probe_source("leaky2")
    assert not probe.ok
    assert "sekret-value-2" not in probe.detail


# ── task 22a: reap_interrupted_pulls (a process crashed/OOM'd mid-pull) ─────────────────────────


def test_reap_marks_a_running_pull_with_no_lock_held_as_failed():
    """No live process holds this source's lock (crashed after it expired, or never held at
    all) — the row is stuck ``running`` and must be reaped."""
    pull_id = catalog.new_pull_id()
    catalog.insert_pull(pull_id, "", "gone", trigger_kind="manual", actor="t", parent_revision=None)
    reaped = dpl.reap_interrupted_pulls()
    assert reaped == [pull_id]
    row = catalog.get_pull(pull_id)
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert "interrupted" in row["error"]


def test_reap_leaves_a_pull_whose_source_lock_is_held_by_another_holder():
    """A live pull (or a crashed holder still inside its lease) must not be touched."""
    from examlops.coordination import get_coordinator
    from examlops.dataplane.pull import pull_lock_key
    from examlops.dataplane.store import source_key

    pull_id = catalog.new_pull_id()
    catalog.insert_pull(
        pull_id, "", "alive", trigger_kind="manual", actor="t", parent_revision=None
    )
    lock_key = pull_lock_key(source_key("", "alive"))
    coord = get_coordinator()
    assert coord.try_lock(lock_key, pull_id, ttl_s=60.0)
    try:
        reaped = dpl.reap_interrupted_pulls()
        assert pull_id not in reaped
        assert catalog.get_pull(pull_id)["status"] == "running"
    finally:
        coord.unlock(lock_key, pull_id)


def test_reap_never_touches_a_succeeded_pull():
    dpl.define_source("s", "counting", spec={})
    result = dpl.run_pull("s")
    assert result.status == "succeeded"
    reaped = dpl.reap_interrupted_pulls()
    assert reaped == []
    assert catalog.get_pull(result.pull_id)["status"] == "succeeded"


# ── task 22a: cleanup_stale_stage_dirs (leftover stage dirs from a crashed process) ─────────────


def test_cleanup_removes_only_stale_dp_prefixed_dirs(tmp_path):
    import os
    import time

    from examlops.dataplane.pull import cleanup_stale_stage_dirs

    cutoff = time.time()

    stale = tmp_path / "dp-oldpull-abc123"
    stale.mkdir()
    old = cutoff - 3600
    os.utime(stale, (old, old))  # predates the cutoff: an earlier process's leftover

    fresh = tmp_path / "dp-newpull-def456"
    fresh.mkdir()
    new = cutoff + 3600
    os.utime(fresh, (new, new))  # postdates the cutoff: this process's own in-flight pull

    unrelated = tmp_path / "not-a-stage-dir"
    unrelated.mkdir()
    os.utime(unrelated, (old, old))  # old, but not `dp-`-prefixed: never touched

    removed = cleanup_stale_stage_dirs(root=tmp_path, cutoff=cutoff)
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_cleanup_never_follows_a_symlink(tmp_path):
    import os
    import time

    from examlops.dataplane.pull import cleanup_stale_stage_dirs

    cutoff = time.time()
    real_target = tmp_path / "real_target"
    real_target.mkdir()
    link = tmp_path / "dp-linked-abc"
    link.symlink_to(real_target, target_is_directory=True)
    old = cutoff - 3600
    os.utime(link, (old, old), follow_symlinks=False)

    removed = cleanup_stale_stage_dirs(root=tmp_path, cutoff=cutoff)
    assert removed == 0
    assert real_target.exists()
    assert link.exists()
