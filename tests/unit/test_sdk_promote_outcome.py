"""ADR 0078 clause 1 — ``examlops.models.promote`` judged by its outcome, not by its argv.

The other promote tests fake the CLI-delegation seam, so they can only show which argv the SDK
builds and how it classifies a message the test itself wrote. These run the real child
(``exa pipeline promote``) against a fake MLflow server and assert what actually happened to the
registry: the alias moved, or it did not.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from examlops.sdk import models
from examlops.sdk.errors import ApprovalRequiredError, NotFoundError


class _FakeMlflow:
    """Just enough of the MLflow REST API for one model with a Staging version."""

    def __init__(self, rmse: float) -> None:
        self.rmse = rmse
        self.alias_posts: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the test output clean
                pass

            def _send(self, code: int, doc: dict) -> None:
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - http.server API
                url = urlparse(self.path)
                q = parse_qs(url.query)
                if url.path.endswith("/registered-models/get"):
                    if q.get("name") != ["jpcp"]:
                        return self._send(404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
                    return self._send(
                        200,
                        {
                            "registered_model": {
                                "name": "jpcp",
                                "aliases": [{"alias": "Staging", "version": "3"}],
                            }
                        },
                    )
                if url.path.endswith("/model-versions/get"):
                    return self._send(
                        200, {"model_version": {"name": "jpcp", "version": "3", "run_id": "r3"}}
                    )
                if url.path.endswith("/runs/get"):
                    return self._send(
                        200,
                        {
                            "run": {
                                "info": {"run_id": "r3"},
                                "data": {"metrics": [{"key": "rmse", "value": fake.rmse}]},
                            }
                        },
                    )
                return self._send(404, {"error_code": "ENDPOINT_NOT_FOUND"})

            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                doc = json.loads(self.rfile.read(length) or b"{}")
                if urlparse(self.path).path.endswith("/registered-models/alias"):
                    fake.alias_posts.append(doc)
                    return self._send(200, {})
                return self._send(404, {"error_code": "ENDPOINT_NOT_FOUND"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _FakeMlflow:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def child_env(monkeypatch, tmp_path):
    """Point parent and child at an empty config/policy dir so only this test's files govern."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("EXAMLOPS_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(cfg_dir / "config.toml"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "sdk-test")
    return cfg_dir


def _promote(model: str = "jpcp", threshold: float = 5.0):
    return models.promote(
        model, metric="rmse", operator="lt", threshold=threshold, confirm=True, timeout=120
    )


def test_a_passing_metric_really_moves_the_alias(child_env, monkeypatch):
    with _FakeMlflow(rmse=4.2) as mlflow:
        monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow.url)
        out = _promote()
    assert out.promoted is True, out
    assert mlflow.alias_posts == [{"name": "jpcp", "alias": "Production", "version": "3"}]


def test_a_failing_metric_leaves_the_registry_untouched(child_env, monkeypatch):
    with _FakeMlflow(rmse=6.0) as mlflow:
        monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow.url)
        out = _promote()
    assert out.promoted is False
    assert mlflow.alias_posts == []


def test_a_negative_threshold_reaches_the_child_as_a_value(child_env, monkeypatch):
    """``-0.5`` must be read as the threshold, not as an unknown short option."""
    with _FakeMlflow(rmse=-1.0) as mlflow:
        monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow.url)
        out = _promote(threshold=-0.5)
    assert out.promoted is True, out
    assert len(mlflow.alias_posts) == 1


def test_an_unknown_model_is_not_found_and_nothing_moves(child_env, monkeypatch):
    with _FakeMlflow(rmse=4.2) as mlflow:
        monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow.url)
        with pytest.raises(NotFoundError):
            _promote("nosuch")
    assert mlflow.alias_posts == []


def test_a_require_approval_rule_in_the_childs_policy_file_is_honoured(child_env, monkeypatch):
    """The parent's pre-check must read the SAME policy file the child enforces.

    The child runs ``--yes``, which answers a require_approval prompt by itself; the SDK's only
    defence is refusing before it starts the child. A rule the child sees but the parent does not
    is a promotion no human approved.
    """
    (child_env / "policy.yaml").write_text(
        "policies:\n"
        "  - name: two-person\n"
        "    action: manual_promote\n"
        "    effect: require_approval\n"
    )
    with _FakeMlflow(rmse=4.2) as mlflow:
        monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow.url)
        with pytest.raises(ApprovalRequiredError):
            _promote()
    assert mlflow.alias_posts == []
