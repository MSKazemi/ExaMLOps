"""ADR 0038 clause 2 — restore the dataset, rebuild against the recorded library commit, and
re-request the recorded scheduler resources.

lakeFS is faked by a real HTTP server on loopback that speaks the three endpoints the module
calls (commit stat, paged ``objects/ls``, object download); nothing is mocked inside the module
under test, so URL building, paging, auth, size/MD5 checks and cleanup are all exercised.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))

COMMIT = "c0ffee" + "0" * 58


class FakeLakeFS:
    """Minimal lakeFS API: one repo, one commit, a page size of 2 to force paging."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {
            "data/a.parquet": b"alpha",
            "data/b.parquet": b"bravo-bytes",
            "schema.json": b"{}",
        }
        self.checksums: dict[str, str] = {}
        self.extra_listing: list[dict] = []
        self.commits = {COMMIT}
        self.auth_seen: list[str | None] = []
        self.page = 2
        #: When set, an object download answers 302 to ``<redirect_to>/blob?path=…``.
        self.redirect_to: str | None = None
        #: Object paths whose download promises more bytes than it sends, then hangs up.
        self.truncate: set[str] = set()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 - silence
                pass

            def _json(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                fake.auth_seen.append(self.headers.get("Authorization"))
                url = urllib.parse.urlsplit(self.path)
                parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
                q = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
                if parts == ["blob"]:  # the redirect target (same server = same origin)
                    body = fake.objects[q["path"]]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return None
                # /api/v1/repositories/<repo>/commits/<id>
                if parts[3:4] == ["ds"] and parts[4:5] == ["commits"]:
                    if parts[5] in fake.commits:
                        return self._json(200, {"id": parts[5]})
                    return self._json(404, {"message": "not found"})
                if parts[4:5] == ["refs"] and parts[5] in fake.commits:
                    if parts[6:] == ["objects", "ls"]:
                        listing = [
                            {
                                "path": p,
                                "path_type": "object",
                                # A truncated object is listed the way a multipart upload is:
                                # no size, a non-MD5 ETag — only Content-Length can catch it.
                                "size_bytes": None if p in fake.truncate else len(b),
                                "checksum": (
                                    "0123abcd-2"
                                    if p in fake.truncate
                                    else fake.checksums.get(p, hashlib.md5(b).hexdigest())
                                ),
                            }
                            for p, b in sorted(fake.objects.items())
                        ] + fake.extra_listing
                        listing.sort(key=lambda o: o["path"])  # lakeFS lists in key order
                        after = q.get("after", "")
                        rest = [o for o in listing if o["path"] > after]
                        page = rest[: fake.page]
                        more = len(rest) > fake.page
                        return self._json(
                            200,
                            {
                                "results": page,
                                "pagination": {
                                    "has_more": more,
                                    "next_offset": page[-1]["path"] if page else "",
                                },
                            },
                        )
                    if parts[6:] == ["objects"]:
                        body = fake.objects.get(q.get("path", ""))
                        if body is None:
                            return self._json(404, {"message": "no object"})
                        if fake.redirect_to:
                            self.send_response(302)
                            loc = f"{fake.redirect_to}/blob?" + urllib.parse.urlencode(
                                {"path": q["path"]}
                            )
                            self.send_header("Location", loc)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return None
                        self.send_response(200)
                        if q.get("path") in fake.truncate:
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body[:-3])
                            self.wfile.flush()
                            self.close_connection = True
                            return None
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return None
                return self._json(404, {"message": "no route"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def lake(monkeypatch):
    f = FakeLakeFS()
    monkeypatch.setenv("EXAMLOPS_LAKEFS_ENDPOINT", f.url)
    monkeypatch.setenv("EXAMLOPS_LAKEFS_ACCESS_KEY_ID", "AKIDTEST")
    monkeypatch.setenv("EXAMLOPS_LAKEFS_SECRET_ACCESS_KEY", "not-a-real-secret")
    yield f
    f.close()


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "core.hooksPath", "/dev/null")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    for name, text in files.items():
        (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "-c", "commit.gpgsign=false", "commit", "-qm", "init")
    return _git(path, "rev-parse", "HEAD")


# The trainer reports what the rebuild handed it, as metrics, so the test reads the outcome.
TRAIN = (
    "import json, os, pathlib\n"
    "d = os.environ.get('EXAMLOPS_REPRO_DATA_DIR')\n"
    "mz = os.environ.get('EXAMLOPS_MODELZOO_DIR')\n"
    "out = {'rmse': 5.0,\n"
    "       'gpus': float(os.environ.get('EXAMLOPS_HPC_GPUS', '-1')),\n"
    "       'restored_files': float(len(list(pathlib.Path(d).rglob('*.parquet')))) if d else -1.0,\n"
    "       'lib_version': float(pathlib.Path(mz, 'lib.txt').read_text()) if mz else -1.0,\n"
    "       'on_flux': 1.0 if os.environ.get('EXAMLOPS_HPC_SCHEDULER') == 'flux' else 0.0}\n"
    "print('EXAMLOPS_REPRO_METRICS=' + json.dumps(out))\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k")
    for var in ("EXAMLOPS_MODELZOO_DIR", "EXAMLOPS_HPC_SCHEDULER", "EXAMLOPS_HPC_GPUS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("EXAMLOPS_LAKEFS_ENDPOINT", raising=False)
    from examlops import platform_db

    platform_db.init_db()
    repo = tmp_path / "repo"
    _init_repo(repo, {"uv.lock": "lock\n", "train.py": TRAIN})
    monkeypatch.chdir(repo)
    return repo


def _record_lakefs_revision(commit: str = COMMIT) -> None:
    from examlops.data.data_assets import record_dataset_revision
    from pipelines.datasets.versioning import DatasetRevision

    record_dataset_revision(
        DatasetRevision(
            backend="minio",
            dataset="PM100",
            revision_id=commit,
            kind="lakefs",
            uri=f"lakefs://ds/{commit}",
        )
    )


def _build(**kw):
    from examlops.reproducibility import build_bundle

    kw.setdefault("metrics", {"rmse": 5.0})
    return build_bundle("M", "1", bom=False, **kw)


def _run(repo, **kw):
    from examlops.reproducibility.execute import execute_reproduction

    kw.setdefault("train_cmd", [sys.executable, "train.py"])
    kw.setdefault("allow_env_drift", True)
    return execute_reproduction("M", "1", repo=repo, **kw)


def _status(res):
    return {s.step: s.status for s in res.steps}


def _detail(res, step):
    return next(s.detail for s in res.steps if s.step == step)


# ── lakeFS module ────────────────────────────────────────────────────────────────────────────


def test_restore_downloads_every_object_across_pages_with_auth(lake, tmp_path):
    from examlops.reproducibility import lakefs

    dest = tmp_path / "restored"
    got = lakefs.restore("ds", COMMIT, dest)
    assert got.ok, got.detail
    assert got.objects == 3 and got.checked_md5 == 3
    assert (dest / "data" / "b.parquet").read_bytes() == b"bravo-bytes"
    assert (dest / "schema.json").read_bytes() == b"{}"
    expected = "Basic " + base64.b64encode(b"AKIDTEST:not-a-real-secret").decode()
    assert set(lake.auth_seen) == {expected}
    assert "not-a-real-secret" not in got.detail


def test_restore_is_idempotent_only_into_an_empty_directory(lake, tmp_path):
    from examlops.reproducibility import lakefs

    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "stray").write_text("x")
    got = lakefs.restore("ds", COMMIT, dest)
    assert not got.ok and "not an empty directory" in got.detail
    assert (dest / "stray").read_text() == "x"  # nothing of the caller's was touched


def test_an_md5_mismatch_fails_and_leaves_nothing_behind(lake, tmp_path):
    from examlops.reproducibility import lakefs

    lake.checksums["data/b.parquet"] = "0" * 32
    dest = tmp_path / "d"
    got = lakefs.restore("ds", COMMIT, dest)
    assert not got.ok and "MD5" in got.detail and "data/b.parquet" in got.detail
    assert list(dest.iterdir()) == []


def test_a_path_that_escapes_the_destination_is_refused(lake, tmp_path):
    from examlops.reproducibility import lakefs

    lake.extra_listing.append({"path": "../../etc/evil", "path_type": "object", "size_bytes": 1})
    dest = tmp_path / "d"
    got = lakefs.restore("ds", COMMIT, dest)
    assert not got.ok and "escapes" in got.detail
    assert not (tmp_path.parent / "etc" / "evil").exists()


def test_caps_bound_a_restore(lake, tmp_path):
    from examlops.reproducibility import lakefs

    got = lakefs.restore("ds", COMMIT, tmp_path / "a", max_objects=2)
    assert not got.ok and "more than 2 objects" in got.detail
    got = lakefs.restore("ds", COMMIT, tmp_path / "b", max_bytes=10)
    assert not got.ok and "cap" in got.detail


def test_a_cross_origin_redirect_never_receives_the_lakefs_credentials(lake, tmp_path):
    """urllib's default redirect handler copies Authorization to ANY target host."""
    from examlops.reproducibility import lakefs

    other = FakeLakeFS()  # a different port = a different origin (e.g. a pre-signed store URL)
    try:
        other.objects = dict(lake.objects)
        lake.redirect_to = other.url
        got = lakefs.restore("ds", COMMIT, tmp_path / "d")
        assert got.ok, got.detail
        assert (tmp_path / "d" / "data" / "b.parquet").read_bytes() == b"bravo-bytes"
        assert other.auth_seen and set(other.auth_seen) == {None}
    finally:
        other.close()


def test_a_same_origin_redirect_keeps_the_credentials(lake, tmp_path):
    from examlops.reproducibility import lakefs

    lake.redirect_to = lake.url
    got = lakefs.restore("ds", COMMIT, tmp_path / "d")
    assert got.ok, got.detail
    assert None not in lake.auth_seen


def test_a_connection_dropped_mid_download_fails_cleanly(lake, tmp_path):
    """http.client returns a short body at a premature EOF instead of raising: with no listed
    size and a non-MD5 ETag, the short file used to be restored as the pinned revision."""
    from examlops.reproducibility import lakefs

    lake.truncate.add("data/b.parquet")
    dest = tmp_path / "d"
    got = lakefs.restore("ds", COMMIT, dest)
    assert not got.ok and "unread" in got.detail and "data/b.parquet" in got.detail
    assert got.objects == 0 and got.files == []
    assert list(dest.iterdir()) == []
    assert "not-a-real-secret" not in got.detail


def test_an_object_colliding_with_a_directory_fails_cleanly(lake, tmp_path):
    from examlops.reproducibility import lakefs

    lake.objects["data"] = b"a file where a directory must be"
    dest = tmp_path / "d"
    got = lakefs.restore("ds", COMMIT, dest)
    assert not got.ok and "restore failed" in got.detail
    assert list(dest.iterdir()) == []


def test_verify_commit_answers_for_present_missing_unset_and_down(lake, monkeypatch):
    from examlops.reproducibility import lakefs

    assert lakefs.verify_commit("ds", COMMIT) is None
    assert "HTTP 404" in lakefs.verify_commit("ds", "f" * 64)
    lake.close()
    assert "unreachable" in lakefs.verify_commit("ds", COMMIT)
    monkeypatch.delenv("EXAMLOPS_LAKEFS_ENDPOINT")
    assert "is not set" in lakefs.verify_commit("ds", COMMIT)


def test_a_non_http_endpoint_is_refused(monkeypatch):
    from examlops.reproducibility import lakefs

    monkeypatch.setenv("EXAMLOPS_LAKEFS_ENDPOINT", "file:///etc")
    assert "http(s)" in lakefs.verify_commit("ds", COMMIT)


# ── verify_bundle: a lakeFS revision is checked against lakeFS, not only the table ───────────


def test_verify_bundle_checks_the_lakefs_commit(env, lake, monkeypatch):
    from examlops.reproducibility import verify_bundle

    _record_lakefs_revision()
    _build(dataset_name="PM100", dataset_revision=COMMIT)
    assert verify_bundle("M", "1", allow_env_drift=True).reproducible
    lake.commits.clear()  # the commit was garbage-collected in lakeFS
    v = verify_bundle("M", "1", allow_env_drift=True)
    assert not v.reproducible and any("HTTP 404" in p for p in v.problems)
    monkeypatch.delenv("EXAMLOPS_LAKEFS_ENDPOINT")
    v = verify_bundle("M", "1", allow_env_drift=True)
    assert not v.reproducible and any("cannot be verified" in p for p in v.problems)


# ── --execute: restore, library checkout, scheduler resources ────────────────────────────────


def test_execute_restores_a_lakefs_dataset_and_hands_it_to_training(env, lake, tmp_path):
    _record_lakefs_revision()
    _build(dataset_name="PM100", dataset_revision=COMMIT)
    dest = tmp_path / "restored"
    res = _run(env, restore_dataset=dest)
    assert res.ok, [(s.step, s.status, s.detail) for s in res.steps]
    assert _status(res)["dataset"] == "ok" and "restored 3 object(s)" in _detail(res, "dataset")
    assert res.dataset_dir == str(dest.resolve())
    assert res.produced_metrics["restored_files"] == 2.0  # the trainer saw the restored data


def test_execute_verifies_a_lakefs_commit_without_restoring(env, lake):
    _record_lakefs_revision()
    _build(dataset_name="PM100", dataset_revision=COMMIT)
    res = _run(env)
    assert _status(res)["dataset"] == "ok" and "exists" in _detail(res, "dataset")
    assert res.dataset_dir is None and res.produced_metrics["restored_files"] == -1.0
    lake.commits.clear()
    res = _run(env)
    assert _status(res)["dataset"] == "failed" and _status(res)["train"] == "not_run"


def test_a_content_revision_cannot_be_restored(env, tmp_path):
    from examlops.data.data_assets import record_dataset_revision
    from pipelines.datasets.versioning import DatasetRevision

    record_dataset_revision(
        DatasetRevision(backend="minio", dataset="PM100", revision_id="abc123", kind="content")
    )
    _build(dataset_name="PM100", dataset_revision="abc123")
    res = _run(env, restore_dataset=tmp_path / "r")
    assert _status(res)["dataset"] == "failed"
    assert "cannot be restored" in _detail(res, "dataset")


def test_a_non_empty_restore_target_is_refused(env, lake, tmp_path):
    _record_lakefs_revision()
    _build(dataset_name="PM100", dataset_revision=COMMIT)
    dest = tmp_path / "busy"
    dest.mkdir()
    (dest / "keep").write_text("mine")
    res = _run(env, restore_dataset=dest)
    assert _status(res)["dataset"] == "failed" and (dest / "keep").read_text() == "mine"


def test_execute_trains_against_the_recorded_modelzoo_commit(env, tmp_path, monkeypatch):
    mz = tmp_path / "zoo"
    sha = _init_repo(mz, {"lib.txt": "1\n"})
    monkeypatch.setenv("EXAMLOPS_MODELZOO_DIR", str(mz))
    _build()
    # The library moved on after the bundle; the rebuild must still use the recorded commit.
    (mz / "lib.txt").write_text("2\n")
    _git(mz, "-c", "commit.gpgsign=false", "commit", "-qam", "later")
    res = _run(env)
    assert res.ok, [(s.step, s.status, s.detail) for s in res.steps]
    assert res.produced_metrics["lib_version"] == 1.0
    assert f"modelzoo {sha[:12]} checked out" in _detail(res, "code")
    assert _git(mz, "worktree", "list").count("\n") == 0  # library worktree cleaned up


def test_execute_refuses_a_modelzoo_commit_it_cannot_find(env, tmp_path, monkeypatch):
    mz = tmp_path / "zoo"
    _init_repo(mz, {"lib.txt": "1\n"})
    monkeypatch.setenv("EXAMLOPS_MODELZOO_DIR", str(mz))
    _build()
    other = tmp_path / "elsewhere"
    _init_repo(other, {"lib.txt": "9\n"})
    monkeypatch.setenv("EXAMLOPS_MODELZOO_DIR", str(other))
    # hide the recorded path too, so no checkout has the commit
    mz.rename(tmp_path / "gone")
    res = _run(env)
    assert _status(res)["code"] == "failed" and "not available" in _detail(res, "code")
    assert _status(res)["train"] == "not_run"


def test_execute_rerequests_recorded_resources_and_scheduler(env):
    from examlops.reproducibility.capture import capture_resources

    _build(resources=capture_resources({"gpus": 4, "nodes": 1}, scheduler="flux"))
    res = _run(env)
    assert res.ok
    assert res.produced_metrics["gpus"] == 4.0 and res.produced_metrics["on_flux"] == 0.0
    assert res.resources == {"EXAMLOPS_HPC_GPUS": "4", "EXAMLOPS_HPC_NODES": "1"}
    assert res.scheduler == "mock" and "re-requested 2" in _detail(res, "train")
    res = _run(env, scheduler="recorded")
    assert res.ok and res.produced_metrics["on_flux"] == 1.0 and res.scheduler == "flux"


def test_the_callers_hpc_environment_never_joins_the_recorded_request(env, monkeypatch):
    """The fixture used to delete EXAMLOPS_HPC_GPUS — seeding the assumption. An operator shell
    with GPUs (or a legacy EXAMLOPS_SLURM_* fallback) set must not leak into a rebuild that
    reports it re-requested the *recorded* resources."""
    from examlops.reproducibility.capture import capture_resources

    monkeypatch.setenv("EXAMLOPS_HPC_GPUS", "8")
    monkeypatch.setenv("EXAMLOPS_SLURM_PARTITION", "caller-partition")
    _build(resources=capture_resources({"nodes": 1}, scheduler="slurm"))
    res = _run(env)
    assert res.ok, res.steps
    assert res.produced_metrics["gpus"] == -1.0  # the original run requested no GPUs
    assert res.resources == {"EXAMLOPS_HPC_NODES": "1"}


def test_a_bundle_with_no_recorded_request_leaves_the_callers_environment_alone(env, monkeypatch):
    """Nothing recorded (a mock run) means nothing to re-request: the caller's settings stand."""
    monkeypatch.setenv("EXAMLOPS_HPC_GPUS", "8")
    _build()
    res = _run(env)
    assert res.ok and res.produced_metrics["gpus"] == 8.0


def test_a_malformed_listed_size_fails_the_restore_cleanly(lake, tmp_path):
    from examlops.reproducibility import lakefs

    lake.extra_listing = [{"path": "x.bin", "path_type": "object", "size_bytes": "many"}]
    dest = tmp_path / "d"
    got = lakefs.restore("ds", COMMIT, dest)
    assert not got.ok and "ValueError" in got.detail
    assert not dest.exists() or list(dest.iterdir()) == []


def test_an_unknown_scheduler_is_refused_not_reported_as_where_training_ran(env):
    from examlops.reproducibility.capture import capture_resources

    _build()
    res = _run(env, scheduler="bogus")
    assert _status(res)["train"] == "failed" and "unknown scheduler" in _detail(res, "train")
    assert res.scheduler != "bogus"
    # a recorded scheduler name is held to the same rule
    _build(resources=capture_resources({"gpus": 1}, scheduler="pbs"))
    res = _run(env, scheduler="recorded")
    assert _status(res)["train"] == "failed" and "'pbs'" in _detail(res, "train")


def test_recorded_scheduler_without_a_record_fails_the_train_step(env):
    _build()
    res = _run(env, scheduler="recorded")
    assert _status(res)["train"] == "failed" and "no scheduler" in _detail(res, "train")


def test_cli_execute_json_reports_scheduler_restore_and_library(env, lake, tmp_path):
    from typer.testing import CliRunner

    from examlops.cli import _output
    from examlops.cli.commands.reproduce_cmd import app

    _record_lakefs_revision()
    _build(dataset_name="PM100", dataset_revision=COMMIT)
    dest = tmp_path / "out"
    _output.json_mode = True
    try:
        r = CliRunner().invoke(
            app,
            [
                "run", "M", "1", "--execute", "--allow-env-drift",
                "--train-cmd", f"{sys.executable} train.py",
                "--restore-dataset", str(dest), "--scheduler", "mock",
            ],
        )  # fmt: skip
    finally:
        _output.json_mode = False
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output[r.output.index("{") :])
    assert doc["dataset_dir"] == str(dest.resolve()) and doc["scheduler"] == "mock"
    assert doc["modelzoo_worktree"] is None


def test_execute_restores_a_dataplane_snapshot(env, tmp_path, monkeypatch):
    pytest.importorskip("pyarrow")
    from tests.unit.test_reproduce_auto import _dp_bundle, _snapshot

    url, root, manifest = _snapshot(tmp_path)
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", url)
    _dp_bundle(url, manifest)
    dest = tmp_path / "restored"
    res = _run(env, restore_dataset=dest)
    assert res.ok, [(s.step, s.status, s.detail) for s in res.steps]
    assert "restored to" in _detail(res, "dataset")
    parts = list(dest.rglob("*.parquet"))
    assert parts and res.produced_metrics["restored_files"] == float(len(parts))
    # a corrupted part is not restored as the pinned revision
    next(root.rglob("part-00000.parquet")).write_bytes(b"garbage")
    res = _run(env, restore_dataset=tmp_path / "again")
    assert _status(res)["dataset"] == "failed" and "does not verify" in _detail(res, "dataset")
