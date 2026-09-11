"""ADR 0130 — data integrity and the pull lifecycle (final-review fix group FB).

Each block is a regression test for one reproduced finding:

- I1  an incremental pull only ever appended, so a file changed in place was duplicated, and a
      changed spec silently mixed old and new data;
- I2  the reaper could fail a pull that committed while it was sweeping, and never reaped a pull
      stuck in ``committing``;
- I3  a failure after the commit turned a committed snapshot into ``failed`` and lost its
      ``dataset_revisions`` row for good;
- I4  the lock lease covered only the read, was never renewed, and prune ignored it;
- I5  ``catalog-rebuild`` rebuilt nothing when ``platform.db`` was lost;
- I11 a pinned revision was never checked against the bytes behind it;
- plus the credential-shape rules carried over from fix group FA.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops import dataplane as dpl  # noqa: E402
from examlops.coordination import get_coordinator  # noqa: E402
from examlops.data import data_assets  # noqa: E402
from examlops.data import dataplane as catalog  # noqa: E402
from examlops.dataplane import pull as pull_mod  # noqa: E402
from examlops.dataplane import store as st  # noqa: E402
from examlops.dataplane.connectors import registry  # noqa: E402
from examlops.dataplane.connectors.base import BaseConnector  # noqa: E402
from examlops.dataplane.types import (  # noqa: E402
    DataplaneError,
    Limits,
    Probe,
    PullInProgress,
    SnapshotNotFound,
    SpecError,
    TableBatch,
)

_ID = pa.schema([("id", pa.int64())])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    registry.reset()
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "1")
    yield
    registry.reset()


def _lose_catalog(monkeypatch, tmp_path: Path) -> None:
    """Simulate a lost ``platform.db`` on either backend: a fresh SQLite file, or an emptied
    Postgres schema (the same reset ``examlops.storage.testing`` does between tests)."""
    from examlops.storage.testing import postgres_backend, reset_state

    if postgres_backend():
        from examlops import platform_db as pdb

        reset_state(pdb)
    else:
        monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "restored-empty.db"))


def _store():
    return dpl.store_from_env()


def _ids(key: str, tmp_path: Path, revision: str = "latest") -> list[int]:
    """Every ``id`` in a committed snapshot, read back from a materialized copy."""
    store = _store()
    local = st.materialize(store, st.resolve(store, key, revision), tmp_path / "cache")
    out: list[int] = []
    for part in sorted(local.rglob("*.parquet")):
        out.extend(pq.read_table(part).column("id").to_pylist())
    return sorted(out)


def _write(path: Path, ids: list[int], *, mtime: float | None = None) -> None:
    pq.write_table(pa.table({"id": ids}), path)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _files_source(name: str, src: Path) -> None:
    dpl.define_source(
        name,
        "files",
        spec={"url": f"file://{src}", "glob": "*.parquet", "incremental": True, "table": "jobs"},
    )


# ── I1 (a): a changed spec never extends the old snapshot ─────────────────────────────────────

_TABLES = {"a": [1, 2, 3], "b": [10, 20]}


class _Keyed(BaseConnector):
    """Rows of ``_TABLES[spec.which]`` past the watermark — an incremental source whose query the
    operator can change (``which``) between pulls."""

    kind = "keyed"
    connection_kinds = ()
    connection_required = False
    supports_incremental = True

    def probe(self, conn, spec=None):
        return Probe(True, "ok")

    def read(self, conn, spec, since, limits):
        after = (since or {}).get("value", 0)
        values = _TABLES[spec["which"]]
        rows = [{"id": v} for v in values if v > after]
        yield TableBatch(
            "rows", pa.RecordBatch.from_pylist(rows, schema=_ID), {"value": max(values)}
        )


def test_a_changed_spec_runs_a_full_pull_instead_of_extending_the_old_data(tmp_path):
    registry.register(_Keyed())
    dpl.define_source("q", "keyed", spec={"which": "a", "incremental": True})
    first = dpl.run_pull("q")
    assert first.row_count == 3
    dpl.define_source("q", "keyed", spec={"which": "b", "incremental": True})
    second = dpl.run_pull("q")
    assert second.status == "succeeded"
    assert second.row_count == 2  # not 3 carried rows of `a` + 2 of `b`
    assert _ids("_global/q", tmp_path) == [10, 20]


# ── I1 (b): file connectors append only NEW files ─────────────────────────────────────────────


def test_repro_inc_a_file_changed_in_place_is_reread_not_appended(tmp_path):
    """The reviewer's ``inc.py``: a 3-row file corrected in place to 4 rows used to give 7."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "jobs.parquet"
    _write(f, [1, 2, 3], mtime=1_700_000_000)
    _files_source("s", src)
    assert dpl.run_pull("s").row_count == 3
    _write(f, [1, 2, 3, 4], mtime=1_700_000_100)
    second = dpl.run_pull("s")
    assert (second.status, second.row_count) == ("succeeded", 4)
    assert _ids("_global/s", tmp_path) == [1, 2, 3, 4]


def test_a_removed_file_forces_a_full_reread(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write(src / "a.parquet", [1, 2], mtime=1_700_000_000)
    _write(src / "b.parquet", [3, 4], mtime=1_700_000_000)
    _files_source("s", src)
    assert dpl.run_pull("s").row_count == 4
    (src / "b.parquet").unlink()
    second = dpl.run_pull("s")
    assert (second.status, second.row_count) == ("succeeded", 2)
    assert _ids("_global/s", tmp_path) == [1, 2]


def test_a_new_file_is_appended_and_the_old_parts_are_carried_not_reread(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write(src / "a.parquet", [1, 2], mtime=1_700_000_000)
    _files_source("s", src)
    first = dpl.run_pull("s")
    _write(src / "b.parquet", [3], mtime=1_700_000_100)
    second = dpl.run_pull("s")
    assert (second.status, second.row_count) == ("succeeded", 3)
    manifest = st.read_manifest(_store(), st.resolve(_store(), "_global/s"))
    assert manifest.parent_revision == first.revision
    owners = sorted(f.object.split("/")[2] for f in manifest.files)
    assert owners == sorted([first.pull_id, second.pull_id])  # a's part carried, b's part new
    assert _ids("_global/s", tmp_path) == [1, 2, 3]


def test_files_connector_signals_a_changed_or_removed_file_and_skips_seen_ones(tmp_path):
    from examlops.dataplane.connectors.files import FilesConnector
    from examlops.dataplane.types import IncrementalInvalidated

    src = tmp_path / "src"
    src.mkdir()
    _write(src / "a.parquet", [1], mtime=1_700_000_000)
    _write(src / "b.parquet", [2], mtime=1_700_000_000)
    spec = {"url": f"file://{src}", "glob": "*.parquet", "incremental": True}
    c = FilesConnector()
    wm = list(c.read(None, spec, None, Limits()))[-1].watermark

    _write(src / "c.parquet", [3], mtime=1_700_000_000)
    new = list(c.read(None, spec, wm, Limits()))
    assert {tb.table for tb in new} == {"c"}  # only the new file is read

    _write(src / "a.parquet", [1, 9], mtime=1_700_000_500)
    with pytest.raises(IncrementalInvalidated, match="changed"):
        list(c.read(None, spec, wm, Limits()))

    _write(src / "a.parquet", [1], mtime=1_700_000_000)  # back to exactly what was seen
    (src / "b.parquet").unlink()
    with pytest.raises(IncrementalInvalidated, match="removed"):
        list(c.read(None, spec, wm, Limits()))


def test_an_invalidated_incremental_restarts_as_a_full_read_under_the_same_pull_id(tmp_path):
    from examlops.dataplane.types import IncrementalInvalidated

    calls: list = []

    class _Invalidating(BaseConnector):
        kind = "invalidating"
        connection_kinds = ()
        connection_required = False
        supports_incremental = True

        def probe(self, conn, spec=None):
            return Probe(True, "ok")

        def read(self, conn, spec, since, limits):
            calls.append(since)
            if since is not None:
                yield TableBatch("rows", pa.RecordBatch.from_pylist([{"id": 7}], schema=_ID))
                raise IncrementalInvalidated("a seen file changed")
            rows = [{"id": 1}, {"id": 2}] if len(calls) == 1 else [{"id": 5}]
            yield TableBatch("rows", pa.RecordBatch.from_pylist(rows, schema=_ID), {"v": 1})

    registry.register(_Invalidating())
    dpl.define_source("inv", "invalidating", spec={"incremental": True})
    dpl.run_pull("inv")
    stage = tmp_path / "stage"
    stage.mkdir()
    pid = catalog.new_pull_id()
    result = dpl.run_pull("inv", pull_id=pid, stage_root=stage)
    assert calls == [None, {"v": 1}, None]
    assert result.pull_id == pid
    manifest = st.read_manifest(_store(), st.resolve(_store(), "_global/inv"))
    assert manifest.pull_id == result.pull_id
    assert {f.object.split("/")[2] for f in manifest.files} == {result.pull_id}  # nothing carried
    assert _ids("_global/inv", tmp_path) == [5]  # the partial incremental row 7 was discarded
    assert list(stage.iterdir()) == []  # both attempts' stage dirs are gone


def _zenodo_state(monkeypatch):
    from examlops.dataplane.connectors import zenodo

    def blob(ids):
        buf = io.BytesIO()
        pq.write_table(pa.table({"id": ids}), buf)
        return buf.getvalue()

    state = {"blobs": {"a.parquet": blob([1]), "b.parquet": blob([2])}, "downloads": []}

    def record():
        return {
            "id": 1,
            "revision": 1,
            "files": [
                {
                    "key": k,
                    "checksum": "md5:" + hashlib.md5(v).hexdigest(),  # noqa: S324 - Zenodo's own
                    "links": {"content": f"https://zenodo.org/api/records/1/files/{k}/content"},
                }
                for k, v in sorted(state["blobs"].items())
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/records/1":
            return httpx.Response(200, json=record())
        key = request.url.path.split("/files/")[1].removesuffix("/content")
        state["downloads"].append(key)
        return httpx.Response(200, content=state["blobs"][key])

    monkeypatch.setattr(
        zenodo,
        "_client_factory",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
    )
    state["blob"] = blob
    return state


def test_zenodo_appends_only_new_files_and_signals_a_changed_or_removed_one(monkeypatch):
    from examlops.dataplane.connectors.zenodo import ZenodoConnector
    from examlops.dataplane.types import IncrementalInvalidated

    state = _zenodo_state(monkeypatch)
    spec = {"record": 1, "files": "*.parquet", "incremental": True}
    c = ZenodoConnector()
    wm = list(c.read(None, spec, None, Limits()))[-1].watermark

    state["blobs"]["c.parquet"] = state["blob"]([3])
    state["downloads"].clear()
    new = list(c.read(None, spec, wm, Limits()))
    assert {tb.table for tb in new} == {"c"}
    assert state["downloads"] == ["c.parquet"]  # a and b are never downloaded again
    assert set(new[-1].watermark["files"]) == {"a.parquet", "b.parquet", "c.parquet"}

    del state["blobs"]["c.parquet"]
    state["blobs"]["a.parquet"] = state["blob"]([1, 9])
    with pytest.raises(IncrementalInvalidated, match="changed"):
        list(c.read(None, spec, wm, Limits()))

    state["blobs"]["a.parquet"] = state["blob"]([1])
    del state["blobs"]["b.parquet"]
    with pytest.raises(IncrementalInvalidated, match="removed"):
        list(c.read(None, spec, wm, Limits()))


def test_zenodo_pull_of_a_changed_file_is_a_full_reread(monkeypatch, tmp_path):
    state = _zenodo_state(monkeypatch)
    dpl.define_source(
        "z", "zenodo", spec={"record": 1, "files": "*.parquet", "incremental": True, "table": "j"}
    )
    assert dpl.run_pull("z").row_count == 2
    state["blobs"]["a.parquet"] = state["blob"]([1, 9])
    second = dpl.run_pull("z")
    assert (second.status, second.row_count) == ("succeeded", 3)  # not 2 + 3
    assert _ids("_global/z", tmp_path) == [1, 2, 9]


# ── I2: the reaper never fails a committed pull, and reaps `committing` ─────────────────────


def test_repro_reap_a_pull_that_finishes_during_the_sweep_stays_succeeded(monkeypatch):
    """The reviewer's ``reap.py``: the live pull finishes between the reaper's snapshot of the
    active rows and its ``try_lock`` — it used to end up ``failed`` despite having a revision."""
    pid = catalog.new_pull_id()
    catalog.insert_pull(pid, "", "src", trigger_kind="manual", actor="x", parent_revision=None)
    coord = get_coordinator()
    key = pull_mod.pull_lock_key("_global/src")
    assert coord.try_lock(key, pid, ttl_s=60)
    real = catalog.list_active_pulls

    def racing():
        rows = real()
        catalog.update_pull(pid, status="succeeded", finished=True, revision="abc")
        coord.unlock(key, pid)
        return rows

    monkeypatch.setattr(catalog, "list_active_pulls", racing)
    assert dpl.reap_interrupted_pulls() == []
    row = catalog.get_pull(pid)
    assert (row["status"], row["revision"], row["error"]) == ("succeeded", "abc", None)


def test_update_pull_only_if_status_leaves_a_row_in_another_state_alone():
    pid = catalog.new_pull_id()
    catalog.insert_pull(pid, "", "s", trigger_kind="manual", actor="x", parent_revision=None)
    assert catalog.update_pull(pid, status="succeeded", finished=True, revision="r") is True
    changed = catalog.update_pull(
        pid, status="failed", error="late", only_if_status=catalog.ACTIVE_PULL_STATUSES
    )
    assert changed is False
    assert catalog.get_pull(pid)["status"] == "succeeded"
    assert "committing" in catalog.ACTIVE_PULL_STATUSES


def test_reaper_fails_a_pull_stuck_in_committing():
    pid = catalog.new_pull_id()
    catalog.insert_pull(pid, "", "gone", trigger_kind="manual", actor="x", parent_revision=None)
    catalog.update_pull(pid, status="committing")
    assert dpl.reap_interrupted_pulls() == [pid]
    row = catalog.get_pull(pid)
    assert row["status"] == "failed" and "interrupted" in row["error"]


class _Counting(BaseConnector):
    kind = "counting"
    connection_kinds = ()
    connection_required = False
    supports_incremental = True
    n = 3

    def probe(self, conn, spec=None):
        return Probe(True, "ok")

    def read(self, conn, spec, since, limits):
        rows = [{"id": i} for i in range(1, self.n + 1)]
        yield TableBatch("rows", pa.RecordBatch.from_pylist(rows, schema=_ID))


def test_reaper_reconciles_a_committing_row_the_store_already_committed(monkeypatch):
    """The catalog write of the final status failed after the store commit: the row stays
    ``committing`` and the reaper settles it from the store as ``succeeded`` — never ``failed``."""
    registry.register(_Counting())
    dpl.define_source("s", "counting", spec={})
    real = catalog.update_pull

    def final_status_fails(pull_id, **kw):
        if kw.get("status") == "succeeded":
            raise RuntimeError("database is locked")
        return real(pull_id, **kw)

    monkeypatch.setattr(catalog, "update_pull", final_status_fails)
    result = dpl.run_pull("s")
    monkeypatch.setattr(catalog, "update_pull", real)
    assert result.status == "succeeded"
    assert catalog.get_pull(result.pull_id)["status"] == "committing"
    assert dpl.reap_interrupted_pulls() == [result.pull_id]
    row = catalog.get_pull(result.pull_id)
    assert (row["status"], row["revision"], row["row_count"]) == ("succeeded", result.revision, 3)
    revs = data_assets.get_dataset_revisions("_global/s", backend="dataplane")
    assert [r["revision_id"] for r in revs] == [result.revision]


# ── I3: bookkeeping after the commit never changes the outcome ──────────────────────────────


def test_repro_postcommit_a_bookkeeping_failure_keeps_the_pull_succeeded_and_heals(monkeypatch):
    """The reviewer's ``postcommit.py``: ``record_dataset_revision`` failing after ``_latest``
    moved used to flip the pull to ``failed`` — and the retry said ``unchanged`` and never wrote
    the revision row."""
    registry.register(_Counting())
    dpl.define_source("s", "counting", spec={})

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    real = data_assets.record_dataset_revision
    monkeypatch.setattr(data_assets, "record_dataset_revision", boom)
    first = dpl.run_pull("s")
    assert first.status == "succeeded"
    assert catalog.get_pull(first.pull_id)["status"] == "succeeded"
    assert st.resolve(_store(), "_global/s").revision == first.revision
    assert data_assets.get_dataset_revisions("_global/s", backend="dataplane") == []

    monkeypatch.setattr(data_assets, "record_dataset_revision", real)
    retry = dpl.run_pull("s")
    assert retry.status == "unchanged"
    revs = data_assets.get_dataset_revisions("_global/s", backend="dataplane")
    assert [r["revision_id"] for r in revs] == [first.revision]  # the missing row healed


def test_an_announce_failure_after_the_commit_does_not_fail_the_pull(monkeypatch):
    registry.register(_Counting())
    dpl.define_source("s", "counting", spec={})

    def boom(*a, **k):
        raise RuntimeError("event bus down")

    monkeypatch.setattr(pull_mod, "_announce", boom)
    result = dpl.run_pull("s")
    assert result.status == "succeeded"
    assert catalog.get_pull(result.pull_id)["status"] == "succeeded"


# ── I4 (a): the lease is renewed for as long as the pull runs ────────────────────────────────


class _RecordingCoord:
    """A coordinator that records every call; ``renew_ok=False`` makes a renewal fail."""

    def __init__(self, renew_ok: bool = True) -> None:
        self.calls: list[tuple] = []
        self.held: dict[str, str] = {}
        self.renew_ok = renew_ok
        self._mu = threading.Lock()

    def try_lock(self, key, holder, ttl_s):
        with self._mu:
            self.calls.append(("lock", key, holder, ttl_s))
            current = self.held.get(key)
            if current == holder and not self.renew_ok:
                return False
            if current in (None, holder):
                self.held[key] = holder
                return True
            return False

    def unlock(self, key, holder):
        with self._mu:
            self.calls.append(("unlock", key, holder, None))
            if self.held.get(key) == holder:
                del self.held[key]


class _Slow(BaseConnector):
    kind = "slow"
    connection_kinds = ()
    connection_required = False
    supports_incremental = False
    seconds = 0.6
    during: list = []

    def probe(self, conn, spec=None):
        return Probe(True, "ok")

    def read(self, conn, spec, since, limits):
        time.sleep(self.seconds)
        for hook in self.during:
            hook()
        yield TableBatch("rows", pa.RecordBatch.from_pylist([{"id": 1}], schema=_ID))


def test_the_lease_is_renewed_with_the_same_holder_and_ttl_until_the_pull_ends(monkeypatch):
    coord = _RecordingCoord()
    monkeypatch.setattr(pull_mod, "get_coordinator", lambda: coord)
    monkeypatch.setattr(pull_mod, "_lease_ttl_s", lambda limits: 0.3)
    registry.register(_Slow())
    dpl.define_source("slow", "slow", spec={})
    result = dpl.run_pull("slow")
    key = pull_mod.pull_lock_key("_global/slow")
    mine = [c for c in coord.calls if c[1] == key and c[2] == result.pull_id]
    locks = [c for c in mine if c[0] == "lock"]
    assert len(locks) >= 3  # the initial lock plus at least two renewals during a 0.6 s read
    assert {c[3] for c in locks} == {0.3}
    assert mine[-1][0] == "unlock"  # nothing renews the lease after it is released
    time.sleep(0.25)
    assert coord.calls[-1][0] == "unlock"  # and the heartbeat thread really stopped


def test_a_pull_whose_lease_was_lost_refuses_to_commit(monkeypatch):
    coord = _RecordingCoord(renew_ok=False)
    monkeypatch.setattr(pull_mod, "get_coordinator", lambda: coord)
    monkeypatch.setattr(pull_mod, "_lease_ttl_s", lambda limits: 0.3)
    registry.register(_Slow())
    dpl.define_source("slow", "slow", spec={})
    with pytest.raises(PullInProgress, match="lease"):
        dpl.run_pull("slow")
    with pytest.raises(SnapshotNotFound):
        st.resolve(_store(), "_global/slow")
    assert catalog.last_pull("", "slow", committed_only=False)["status"] == "failed"


def test_a_pull_longer_than_its_ttl_keeps_the_real_lock_and_is_not_reaped(monkeypatch):
    """Against the real (DB) coordinator: 3.6 s into a pull with a 3 s lease, a twin still cannot
    take the lock and the reaper leaves the pull alone — the heartbeat kept the lease alive."""
    monkeypatch.setattr(pull_mod, "_lease_ttl_s", lambda limits: 3.0)
    seen: dict = {}
    key = pull_mod.pull_lock_key("_global/slow")

    def probe_twin_and_reaper():
        seen["twin"] = get_coordinator().try_lock(key, "twin", ttl_s=60)
        seen["reaped"] = dpl.reap_interrupted_pulls()

    slow = _Slow()
    slow.seconds = 3.6
    slow.during = [probe_twin_and_reaper]
    registry.register(slow)
    dpl.define_source("slow", "slow", spec={})
    result = dpl.run_pull("slow")
    assert seen == {"twin": False, "reaped": []}
    assert result.status == "succeeded"


# ── I4 (b)/(c) + I5 (c): prune takes the source's lock and refuses a blind prune ──────────


def _three_pulls(name: str = "s") -> list:
    """Three committed pulls that started well past prune's orphan grace (ids are start times)."""
    registry.register(_Counting())
    dpl.define_source(name, "counting", spec={})
    base = time.time_ns() - (st._ORPHAN_AGE_S + 600) * 1_000_000_000
    out = []
    for n in (1, 2, 3):
        _Counting.n = n
        try:
            out.append(dpl.run_pull(name, pull_id=f"{base + n * 1_000_000:016x}abcdef"))
        finally:
            _Counting.n = 3
    return out


def _age(store: st.DatasetStore, key: str, pull_id: str, seconds: float) -> None:
    then = time.time() - seconds
    for path in Path(store.root, key, pull_id).rglob("*"):
        os.utime(path, (then, then))


def test_prune_source_refuses_while_a_pull_holds_the_source_lock():
    pulls = _three_pulls()
    store = _store()
    for r in pulls:
        _age(store, "_global/s", r.pull_id, st._ORPHAN_AGE_S + 60)
    coord = get_coordinator()
    key = pull_mod.pull_lock_key("_global/s")
    assert coord.try_lock(key, "a-running-pull", ttl_s=60)
    with pytest.raises(PullInProgress):
        pull_mod.prune_source("", "s", keep=1, pinned=set())
    assert len(store.ls_dirs("_global/s")) == 4  # three pulls + _revisions, untouched
    coord.unlock(key, "a-running-pull")
    removed = pull_mod.prune_source("", "s", keep=1, pinned=set())
    assert sorted(removed) == sorted(r.pull_id for r in pulls[:2])
    assert coord.try_lock(key, "next", ttl_s=60)  # prune released the lock


def test_prune_refuses_when_the_catalog_lost_its_revision_rows(tmp_path, monkeypatch):
    pulls = _three_pulls()
    store = _store()
    for r in pulls:
        _age(store, "_global/s", r.pull_id, st._ORPHAN_AGE_S + 60)
    _lose_catalog(monkeypatch, tmp_path)  # platform.db lost
    with pytest.raises(DataplaneError, match="catalog-rebuild"):
        pull_mod.prune_source("", "s", keep=1, pinned=set())
    assert len(store.ls_dirs("_global/s")) == 4
    removed = pull_mod.prune_source("", "s", keep=1, pinned=set(), force=True)
    assert len(removed) == 2


@pytest.fixture(params=["fsspec-local", "arrow-wrapper"])
def any_store(request, tmp_path):
    if request.param == "arrow-wrapper":
        from fsspec.implementations.arrow import ArrowFSWrapper
        from pyarrow.fs import LocalFileSystem

        root = tmp_path / "wstore"
        fs = ArrowFSWrapper(LocalFileSystem(), skip_instance_cache=True)
        return st.DatasetStore(fs, str(root), uri_prefix=f"file://{root}")
    return st.DatasetStore.from_url(f"file://{tmp_path / 'wstore'}")


def test_prune_grace_counts_from_the_newest_object_not_the_pull_start(any_store, tmp_path):
    """A pull that started long ago and is uploading its parts right now is not garbage."""
    key = "_global/s"
    w = st.SnapshotWriter(tmp_path / "stage", limits=Limits())
    w.write(TableBatch("jobs", pa.RecordBatch.from_pylist([{"id": 1}], schema=_ID)))
    st.publish(
        any_store,
        key,
        staged=w.close(),
        parent=None,
        connector="x",
        connection=None,
        spec_hash="h",
        watermark={},
        pull_id=catalog.new_pull_id(),
        incremental=False,
    )
    started = time.time_ns() - (st._ORPHAN_AGE_S + 600) * 1_000_000_000
    inflight = f"{started:016x}abcdef"
    any_store.write_text(f"{key}/{inflight}/jobs/part-00000.parquet", "just uploaded")
    assert inflight not in st.prune(any_store, key, keep=0, pinned=set())
    assert any_store.exists(f"{key}/{inflight}/jobs/part-00000.parquet")

    _age(any_store, key, inflight, st._ORPHAN_AGE_S + 60)
    assert inflight in st.prune(any_store, key, keep=0, pinned=set())
    assert not any_store.exists(f"{key}/{inflight}")


def test_newest_mtime_falls_back_to_none_when_the_listing_has_no_times():
    class _NoTimes:
        def exists(self, path):
            return True

        def find(self, path, detail=False):
            return {f"{path}/a": {"name": f"{path}/a", "size": 1, "type": "file"}}

    store = st.DatasetStore(_NoTimes(), "/r", uri_prefix="mem://r")
    assert store.newest_mtime("_global/s/p") is None


# ── I5 (a)/(b): catalog-rebuild walks the store; linking a run upserts ───────────────────────


def test_catalog_rebuild_restores_every_pull_and_revision_from_the_store(tmp_path, monkeypatch):
    pulls = _three_pulls()
    dpl.define_source("t", "counting", project="research", spec={})
    other = dpl.run_pull("t", project="research")
    _lose_catalog(monkeypatch, tmp_path)  # platform.db lost
    assert catalog.list_pulls() == []

    report = pull_mod.rebuild_catalog(actor="t")
    assert report["pulls_restored"] == 4
    assert report["revisions"] == 4
    restored = {r["id"]: r for r in catalog.list_pulls(limit=50)}
    assert set(restored) == {r.pull_id for r in pulls} | {other.pull_id}
    for r in pulls:
        row = restored[r.pull_id]
        assert (row["status"], row["revision"], row["row_count"]) == (
            "succeeded",
            r.revision,
            r.row_count,
        )
        assert row["finished_at"] and row["started_at"] and row["source"] == "s"
    assert restored[other.pull_id]["project"] == "research"
    revs = data_assets.get_dataset_revisions("_global/s", backend="dataplane")
    assert {r["revision_id"] for r in revs} == {r.revision for r in pulls}  # not just latest
    assert catalog.last_pull("", "s")["id"] == pulls[-1].pull_id
    to_register = {(s["project"], s["name"]): s for s in report["sources_to_register"]}
    assert set(to_register) == {("", "s"), ("research", "t")}
    assert to_register[("", "s")]["connector"] == "counting"

    again = pull_mod.rebuild_catalog(actor="t")  # idempotent
    assert again["pulls_restored"] == 0
    assert len(catalog.list_pulls(limit=50)) == 4
    assert len(data_assets.get_dataset_revisions("_global/s", backend="dataplane")) == 3

    # the rebuilt catalog is no longer "lost": prune proceeds without --force
    for r in pulls:
        _age(_store(), "_global/s", r.pull_id, st._ORPHAN_AGE_S + 60)
    assert len(pull_mod.prune_source("", "s", keep=1, pinned=set())) == 2


def test_catalog_rebuild_dry_run_writes_nothing(tmp_path, monkeypatch):
    _three_pulls()
    _lose_catalog(monkeypatch, tmp_path)
    report = pull_mod.rebuild_catalog(dry_run=True)
    assert report["dry_run"] is True and report["revisions"] == 3
    assert catalog.list_pulls() == []
    assert data_assets.get_dataset_revisions("_global/s", backend="dataplane") == []


def test_link_dataset_revision_run_inserts_a_missing_row_and_keeps_first_run_wins():
    rev = "c" * 64
    data_assets.link_dataset_revision_run("dataplane", "_global/s", rev, "run-A")
    data_assets.link_dataset_revision_run("dataplane", "_global/s", rev, "run-B")
    rows = data_assets.get_dataset_revisions("_global/s", backend="dataplane")
    assert [(r["revision_id"], r["mlflow_run_id"]) for r in rows] == [(rev, "run-A")]


runner = CliRunner()


def _cli(args):
    from examlops.cli.main import app

    return runner.invoke(app, ["--json", *args])


def test_cli_prune_refuses_a_blind_prune_and_force_overrides(tmp_path, monkeypatch):
    pulls = _three_pulls()
    for r in pulls:
        _age(_store(), "_global/s", r.pull_id, st._ORPHAN_AGE_S + 60)
    _lose_catalog(monkeypatch, tmp_path)
    refused = _cli(["dataplane", "prune", "s", "--keep", "1"])
    assert refused.exit_code != 0 and "catalog-rebuild" in refused.output
    forced = _cli(["dataplane", "prune", "s", "--keep", "1", "--force"])
    assert forced.exit_code == 0, forced.output
    assert len(json.loads(forced.output)["removed"]) == 2


def test_cli_catalog_rebuild_reports_sources_to_register(tmp_path, monkeypatch):
    _three_pulls()
    _lose_catalog(monkeypatch, tmp_path)
    out = _cli(["dataplane", "catalog-rebuild"])
    assert out.exit_code == 0, out.output
    body = json.loads(out.output)
    assert body["pulls_restored"] == 3 and body["snapshots"] == 3
    assert [s["name"] for s in body["sources_to_register"]] == ["s"]


# ── I11: a pinned revision is verified when it is read ───────────────────────────────────────


def test_materialize_refuses_a_manifest_whose_digests_do_not_hash_to_the_revision(tmp_path):
    from examlops.data.content_hash import file_digest
    from examlops.dataplane.types import SnapshotIntegrityError

    registry.register(_Counting())
    dpl.define_source("s", "counting", spec={})
    dpl.run_pull("s")
    store = _store()
    ref = st.resolve(store, "_global/s")
    manifest = st.read_manifest(store, ref)
    # swap the data behind the pinned id and rewrite the manifest so every per-file check passes
    forged = tmp_path / "forged.parquet"
    pq.write_table(pa.table({"id": [666]}), forged)
    store.put_file(forged, manifest.files[0].object)
    f0 = replace(manifest.files[0], sha256=file_digest(forged), bytes=forged.stat().st_size, rows=1)
    store.write_text(ref.manifest_key, replace(manifest, files=(f0,)).to_json())
    with pytest.raises(SnapshotIntegrityError, match="revision"):
        st.materialize(store, ref, tmp_path / "cache")
    assert not (tmp_path / "cache" / ref.revision).exists()


@pytest.mark.parametrize("bad", ["../_latest", "abc", "A" * 64, "g" * 64, "a" * 63, "a/" * 32])
def test_resolve_accepts_only_latest_or_a_full_revision_id(bad):
    registry.register(_Counting())
    dpl.define_source("s", "counting", spec={})
    dpl.run_pull("s")
    with pytest.raises(SpecError, match="revision"):
        st.resolve(_store(), "_global/s", bad)


def test_resolve_still_takes_latest_and_reports_an_unknown_full_id_as_not_found():
    registry.register(_Counting())
    dpl.define_source("s", "counting", spec={})
    result = dpl.run_pull("s")
    store = _store()
    for latest in (None, "", "latest"):
        assert st.resolve(store, "_global/s", latest).revision == result.revision
    assert st.resolve(store, "_global/s", result.revision).revision == result.revision
    with pytest.raises(SnapshotNotFound):
        st.resolve(store, "_global/s", "0" * 64)


# ── carried from FA: credentials in URL userinfo and in spec.headers ─────────────────────────


@pytest.mark.parametrize(
    ("spec", "where"),
    [
        ({"url": "https://svc:PW-VALUE-1@host.example/x.csv"}, "spec.url"),
        ({"url": "sftp://u:PW-VALUE-1@h.example/drops/*.jsonl"}, "spec.url"),
        ({"base_url": "https://a:PW-VALUE-1@api.example/v1"}, "spec.base_url"),
        ({"nested": {"endpoint": "http://k:PW-VALUE-1@minio:9000"}}, "spec.nested.endpoint"),
        (
            {"mirrors": ["https://ok.example/", "https://u:PW-VALUE-1@m.example/"]},
            "spec.mirrors[1]",
        ),
    ],
)
def test_a_password_in_url_userinfo_is_refused_without_echoing_it(spec, where):
    with pytest.raises(SpecError) as info:
        pull_mod._reject_secret_keys(spec)
    assert where in str(info.value)
    assert "PW-VALUE-1" not in str(info.value)


def test_a_bare_username_in_a_url_is_fine():
    assert pull_mod._reject_secret_keys({"url": "sftp://ingest@h.example/drops/*.csv"}) is None
    assert pull_mod._reject_secret_keys({"query": "SELECT 'a://b:c@d' AS x"}) is None


def test_define_source_refuses_a_url_with_a_password_before_storing_anything():
    with pytest.raises(SpecError, match="spec.url"):
        dpl.define_source("u", "files", spec={"url": "https://u:SECRETPW@h.example/x.csv"})
    assert catalog.get_source("u", "") is None


@pytest.mark.parametrize(
    "header",
    [
        "X-Auth-Key",
        "Ocp-Apim-Subscription-Key",
        "X-Functions-Key",
        "Signature",
        "X-Amz-Signature",
        "oauth2",
        "jwt",
        "session_id",
        "X-Session-Token",
    ],
)
def test_credential_headers_are_refused_inside_spec_headers(header):
    with pytest.raises(SpecError) as info:
        pull_mod._reject_secret_keys({"headers": {header: "HDR-VALUE"}})
    assert f"spec.headers.{header}" in str(info.value)
    assert "HDR-VALUE" not in str(info.value)


@pytest.mark.parametrize("header", ["Accept", "User-Agent", "Content-Type", "X-Request-Id"])
def test_ordinary_headers_still_pass_the_stricter_header_rule(header):
    assert pull_mod._reject_secret_keys({"headers": {header: "v"}}) is None


def test_the_stricter_rule_is_scoped_to_headers():
    # Outside spec.headers the original list applies, so ordinary keys are not false positives.
    assert pull_mod._reject_secret_keys({"session_id": "x", "signature_col": "sig"}) is None


# ── fix round 1 ───────────────────────────────────────────────────────────────────────────────


class _Sized(BaseConnector):
    """``n`` rows, full reads only — each pull with a new ``n`` is a new revision."""

    kind = "sized"
    connection_kinds = ()
    connection_required = False
    supports_incremental = False
    n = 1

    def probe(self, conn, spec=None):
        return Probe(True, "ok")

    def read(self, conn, spec, since, limits):
        rows = [{"id": i} for i in range(type(self).n)]
        yield TableBatch("rows", pa.RecordBatch.from_pylist(rows, schema=_ID))


def _sized_pull(n: int):
    _Sized.n = n
    try:
        return dpl.run_pull("s")
    finally:
        _Sized.n = 1


def test_repro_p2_rebuild_of_an_older_revision_does_not_make_a_model_stale(monkeypatch):
    """The re-review's p2: R2's index write failed, R3 indexed, a model built on R3; re-indexing the
    older R2 during catalog-rebuild used to bump the dataset asset (2 → 3) and stale the model."""
    from examlops import platform_db
    from examlops.assets import asset_status

    registry.register(_Sized())
    dpl.define_source("s", "sized", spec={})
    _sized_pull(1)
    real = data_assets.record_dataset_revision

    def locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(data_assets, "record_dataset_revision", locked)
    assert _sized_pull(2).status == "succeeded"
    monkeypatch.setattr(data_assets, "record_dataset_revision", real)
    time.sleep(1.1)  # manifest created_at has second resolution: R3 is strictly newer than R2
    _sized_pull(3)
    version = platform_db.get_asset("_global/s")["current_version"]
    platform_db.register_asset("model-m", "model", ["_global/s"], description="")
    platform_db.bump_asset_version("model-m", {"_global/s": version})
    assert asset_status("model-m").fresh

    report = pull_mod.rebuild_catalog(actor="t")
    assert report["revisions"] == 3
    assert platform_db.get_asset("_global/s")["current_version"] == version  # no bump
    assert asset_status("model-m").fresh
    revs = data_assets.get_dataset_revisions("_global/s", backend="dataplane")
    assert len(revs) == 3  # R2 was still indexed — just without announcing a change


def test_rebuild_of_a_lost_catalog_declares_the_dataset_changed_exactly_once(tmp_path, monkeypatch):
    import examlops.assets as assets_mod

    _three_pulls()
    _lose_catalog(monkeypatch, tmp_path)
    calls: list[str] = []
    real = assets_mod.mark_source_changed

    def counting(name, **kw):
        calls.append(name)
        return real(name, **kw)

    monkeypatch.setattr(assets_mod, "mark_source_changed", counting)
    report = pull_mod.rebuild_catalog(actor="t")
    assert report["revisions"] == 3
    assert calls == ["_global/s"]  # one bump (and one lineage event), not one per revision
    pull_mod.rebuild_catalog(actor="t")
    assert calls == ["_global/s"]  # a second, no-op rebuild announces nothing


def test_repro_p3_a_spec_change_with_unchanged_data_does_not_keep_every_later_pull_full():
    """The re-review's p3: the full pull after a spec change came back `unchanged` and kept the
    parent manifest (old spec_hash), so every later pull ran full again."""
    calls: list = []

    class _K(BaseConnector):
        kind = "k"
        connection_kinds = ()
        connection_required = False
        supports_incremental = True

        def probe(self, conn, spec=None):
            return Probe(True, "ok")

        def read(self, conn, spec, since, limits):
            calls.append(since)
            after = (since or {}).get("value", 0)
            rows = [{"id": v} for v in (1, 2, 3) if v > after]
            yield TableBatch("r", pa.RecordBatch.from_pylist(rows, schema=_ID), {"value": 3})

    registry.register(_K())
    dpl.define_source("q", "k", spec={"incremental": True})
    first = dpl.run_pull("q")
    src = dpl.define_source("q", "k", spec={"incremental": True, "rate_limit_per_sec": 5})
    second = dpl.run_pull("q")
    assert calls[-1] is None  # the spec changed: one full read
    assert (second.status, second.revision) == ("succeeded", first.revision)
    latest = st.read_manifest(_store(), st.resolve(_store(), "_global/q"))
    assert (latest.spec_hash, latest.pull_id) == (src.spec_hash, second.pull_id)
    for _ in range(2):
        again = dpl.run_pull("q")
        assert again.status == "unchanged"
        assert calls[-1] == {"value": 3}  # incremental again


def test_prune_keeps_a_revision_two_pulls_share_after_a_spec_only_change(tmp_path):
    registry.register(_Counting())
    base = time.time_ns() - (st._ORPHAN_AGE_S + 600) * 1_000_000_000
    dpl.define_source("s", "counting", spec={})
    first = dpl.run_pull("s", pull_id=f"{base:016x}abcdef")
    dpl.define_source("s", "counting", spec={"note": "same data"})
    second = dpl.run_pull("s", pull_id=f"{base + 1_000_000:016x}abcdef")
    assert second.status == "succeeded" and second.revision == first.revision
    _age(_store(), "_global/s", first.pull_id, st._ORPHAN_AGE_S + 60)
    assert pull_mod.prune_source("", "s", keep=1, pinned=set()) == [first.pull_id]
    assert st.resolve(_store(), "_global/s", first.revision).pull_id == second.pull_id
    assert _ids("_global/s", tmp_path, first.revision) == [1, 2, 3]


def _committing_row_with_manifest_but_no_latest(monkeypatch, tmp_path: Path, n: int = 1):
    """A pull that died after writing its manifest and revision pointer, before `_latest`."""
    registry.register(_Sized())
    dpl.define_source("s", "sized", spec={})
    pid = catalog.new_pull_id()
    catalog.insert_pull(pid, "", "s", trigger_kind="manual", actor="x", parent_revision=None)
    catalog.update_pull(pid, status="committing")
    stage = tmp_path / f"stage-{pid}"
    w = st.SnapshotWriter(stage, limits=Limits())
    w.write(
        TableBatch("rows", pa.RecordBatch.from_pylist([{"id": i} for i in range(n)], schema=_ID))
    )
    real_write = st.DatasetStore.write_text

    def crash_on_latest(self, key, text):
        if key.endswith("/_latest"):
            raise RuntimeError("process killed")
        return real_write(self, key, text)

    monkeypatch.setattr(st.DatasetStore, "write_text", crash_on_latest)
    with pytest.raises(RuntimeError):
        st.publish(
            _store(),
            "_global/s",
            staged=w.close(),
            parent=None,
            connector="sized",
            connection=None,
            spec_hash=dpl.get_source_def("s").spec_hash,
            watermark={},
            pull_id=pid,
            incremental=False,
        )
    monkeypatch.setattr(st.DatasetStore, "write_text", real_write)
    return pid


def test_reaper_leaves_a_committing_row_when_the_store_is_transiently_unreadable(
    monkeypatch, tmp_path
):
    pid = _committing_row_with_manifest_but_no_latest(monkeypatch, tmp_path)
    real_exists = st.DatasetStore.exists

    def flaky(self, key):
        raise ConnectionError("store endpoint timed out")

    monkeypatch.setattr(st.DatasetStore, "exists", flaky)
    assert dpl.reap_interrupted_pulls() == []
    assert catalog.get_pull(pid)["status"] == "committing"  # left for the next tick
    monkeypatch.setattr(st.DatasetStore, "exists", real_exists)
    assert dpl.reap_interrupted_pulls() == [pid]
    assert catalog.get_pull(pid)["status"] == "succeeded"


def test_reaper_completes_the_commit_by_advancing_latest(monkeypatch, tmp_path):
    pid = _committing_row_with_manifest_but_no_latest(monkeypatch, tmp_path)
    with pytest.raises(SnapshotNotFound):
        st.resolve(_store(), "_global/s")
    assert dpl.reap_interrupted_pulls() == [pid]
    ref = st.resolve(_store(), "_global/s")
    assert ref.pull_id == pid
    assert ref.revision == catalog.get_pull(pid)["revision"]


def test_reaper_does_not_move_latest_back_to_an_older_snapshot(monkeypatch, tmp_path):
    pid = _committing_row_with_manifest_but_no_latest(monkeypatch, tmp_path, n=1)
    time.sleep(1.1)
    newer = _sized_pull(5)  # a later pull committed after the crash
    assert dpl.reap_interrupted_pulls() == [pid]
    assert catalog.get_pull(pid)["status"] == "succeeded"
    assert st.resolve(_store(), "_global/s").pull_id == newer.pull_id


def test_recording_a_revision_fills_a_link_only_placeholder_and_keeps_the_run(monkeypatch):
    from types import SimpleNamespace

    import examlops.assets as assets_mod

    calls: list[str] = []
    monkeypatch.setattr(assets_mod, "mark_source_changed", lambda name, **kw: calls.append(name))
    rev = "d" * 64
    data_assets.link_dataset_revision_run("dataplane", "_global/s", rev, "run-A")
    assert calls == []
    full = SimpleNamespace(
        backend="dataplane",
        dataset="_global/s",
        revision_id=rev,
        kind="dataplane",
        uri="file:///store/_global/s/p/_manifest.json",
        schema_hash="sh",
    )
    data_assets.record_dataset_revision(full, row_count=7, byte_count=70, actor="t")
    (row,) = data_assets.get_dataset_revisions("_global/s", backend="dataplane")
    assert (row["uri"], row["row_count"], row["byte_count"], row["mlflow_run_id"]) == (
        full.uri,
        7,
        70,
        "run-A",
    )
    assert calls == ["_global/s"]  # the placeholder became a real revision: announced once
    data_assets.record_dataset_revision(full, row_count=9, byte_count=90, actor="t")
    (row,) = data_assets.get_dataset_revisions("_global/s", backend="dataplane")
    assert row["row_count"] == 7 and calls == ["_global/s"]  # a real row is never rewritten


def test_one_bad_pull_dir_cannot_abort_a_rebuild(tmp_path, monkeypatch):
    pulls = _three_pulls()
    _lose_catalog(monkeypatch, tmp_path)
    real = pull_mod._pull_started_at
    bad = pulls[1].pull_id

    def odd(pid, manifest):
        if pid == bad:
            raise ValueError("unparseable pull dir name")
        return real(pid, manifest)

    monkeypatch.setattr(pull_mod, "_pull_started_at", odd)
    report = pull_mod.rebuild_catalog(actor="t")
    assert report["skipped"] == 1
    assert {r["id"] for r in catalog.list_pulls()} == {pulls[0].pull_id, pulls[2].pull_id}


def test_pull_started_at_tolerates_an_odd_name_and_a_malformed_created_at():
    m = st.SnapshotManifest(
        source="_global/s",
        connector="x",
        connection=None,
        spec_hash="h",
        revision="0" * 64,
        schema_hash="",
        files=(),
        tables=(),
        row_count=0,
        byte_count=0,
        created_at=12345,  # type: ignore[arg-type]  # a hand-edited / corrupt manifest
    )
    assert pull_mod._pull_started_at("legacy-pull", m) is None
    assert pull_mod._pull_started_at("zzzzzzzzzzzzzzzzzz", m) is None


def test_heartbeat_stop_is_bounded_and_a_late_renewal_releases_the_lock(monkeypatch):
    release = threading.Event()

    class _Slow(_RecordingCoord):
        def try_lock(self, key, holder, ttl_s):
            if any(c[0] == "lock" for c in self.calls):  # every renewal hangs until released
                release.wait(5)
            return super().try_lock(key, holder, ttl_s)

    coord = _Slow()
    assert coord.try_lock("k", "me", 0.3)
    monkeypatch.setattr(pull_mod, "_HEARTBEAT_JOIN_S", 0.2)
    hb = pull_mod._LeaseHeartbeat(coord, "k", "me", 0.3).start()
    time.sleep(0.2)  # the first renewal is now in flight and stuck
    began = time.monotonic()
    hb.stop()
    assert time.monotonic() - began < 1.0  # stop() did not wait for the stuck renewal
    coord.unlock("k", "me")  # the owner releases, as run_pull's finally does
    release.set()
    hb._thread.join(timeout=3)
    assert not hb._thread.is_alive()
    assert [c[0] for c in coord.calls] == ["lock", "unlock", "lock", "unlock"]
    assert "k" not in coord.held  # the late renewal re-took the lock and gave it straight back


def test_a_pull_whose_latest_moved_underneath_it_refuses_to_commit(tmp_path):
    """A twin committed while this pull ran (its lease lapsed unnoticed): committing now would
    silently replace the twin's snapshot with one built on a stale parent."""
    key = "_global/s"

    class _Twin(_Counting):
        kind = "twin"

        def read(self, conn, spec, since, limits):
            w = st.SnapshotWriter(tmp_path / "twin-stage", limits=Limits())
            w.write(TableBatch("rows", pa.RecordBatch.from_pylist([{"id": 99}], schema=_ID)))
            st.publish(
                _store(),
                key,
                staged=w.close(),
                parent=None,
                connector="twin",
                connection=None,
                spec_hash="other",
                watermark={},
                pull_id=catalog.new_pull_id(),
                incremental=False,
            )
            yield from super().read(conn, spec, since, limits)

    registry.register(_Twin())
    dpl.define_source("s", "twin", spec={})
    with pytest.raises(PullInProgress, match="_latest"):
        dpl.run_pull("s")
    assert catalog.last_pull("", "s", committed_only=False)["status"] == "failed"
    assert st.read_manifest(_store(), st.resolve(_store(), key)).connector == "twin"


@pytest.mark.parametrize(
    "param",
    [
        "sig",
        "signature",
        "X-Amz-Signature",
        "X-Amz-Credential",
        "token",
        "access_token",
        "apikey",
        "key",
        "code",
        "password",
        "secret",
    ],
)
def test_a_credential_in_a_url_query_is_refused_without_echoing_it(param):
    spec = {"url": f"https://h.example/export.csv?page=2&{param}=QUERY-VALUE-1"}
    with pytest.raises(SpecError) as info:
        pull_mod._reject_secret_keys(spec)
    assert "spec.url" in str(info.value)
    assert "QUERY-VALUE-1" not in str(info.value)


def test_ordinary_url_query_parameters_are_fine():
    url = "https://h.example/api?page=2&q=jobs&keyword=gpu&country_code=DE&author=x&format=csv"
    assert pull_mod._reject_secret_keys({"url": url}) is None


# ── a dangling _latest (store damaged out of band) must not block the source forever ──────────


def test_a_dangling_latest_pointer_does_not_refuse_every_later_pull(tmp_path):
    src = tmp_path / "drop"
    src.mkdir()
    _write(src / "a.parquet", [1, 2])
    _files_source("dang", src)
    first = dpl.run_pull("dang")
    assert first.status == "succeeded"

    # Damage the store: the manifest _latest names disappears, the pointer stays.
    manifests = list((tmp_path / "store" / "_global" / "dang").rglob("_manifest.json"))
    assert len(manifests) == 1
    manifests[0].unlink()

    again = dpl.run_pull("dang")
    assert again.status == "succeeded"  # a full re-read, not "_latest moved … not committing"
    assert _ids("_global/dang", tmp_path) == [1, 2]
