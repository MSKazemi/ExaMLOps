"""ADR 0031 clause 1 — the ``k8s`` applier (Deployment ``scale`` subresource).

No cluster is available here, so a fake API server holds Deployments and HPAs and answers the exact
paths/verbs/bodies the real API server does (``GET/PATCH …/deployments/{name}/scale`` with
``application/merge-patch+json``, paged ``…/horizontalpodautoscalers``). One test also drives the
real urllib transport against a loopback HTTP server, so the wire path is exercised, not only a
function call.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.autoscale import set_policy  # noqa: E402
from examlops.autoscale.controller import (  # noqa: E402
    AutoscaleController,
    ScaleApplierUnavailable,
    ScaleApplyError,
    Signals,
    make_applier,
)
from examlops.autoscale.k8s import (  # noqa: E402
    K8sConfigError,
    KubernetesApplier,
    http_transport,
)


class FakeApiServer:
    """Deployments + HPAs of one namespace, answering like kube-apiserver."""

    def __init__(self, deployments=None, hpas=None, *, page=None, forbid_patch=False):
        self.deployments = dict(deployments or {})  # name -> replicas
        self.ready = {}  # name -> readyReplicas
        self.hpas = list(hpas or [])  # (hpa name, target deployment)
        self.page = page
        self.forbid_patch = forbid_patch
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        base = "/apis/apps/v1/namespaces/ml/deployments/"
        if path.startswith("/apis/autoscaling/v2/namespaces/ml/horizontalpodautoscalers"):
            start = 0
            if "continue=" in path:
                start = int(path.split("continue=")[1])
            size = self.page or len(self.hpas) or 1
            chunk = self.hpas[start : start + size]
            nxt = start + size
            items = [
                {
                    "metadata": {"name": n},
                    "spec": {"scaleTargetRef": {"kind": "Deployment", "name": t}},
                }
                for n, t in chunk
            ]
            meta = {"continue": str(nxt)} if nxt < len(self.hpas) else {}
            return 200, {"items": items, "metadata": meta}
        if path.startswith(base):
            rest = path[len(base) :]
            name, _, sub = rest.partition("/")
            if name not in self.deployments:
                return 404, {"kind": "Status", "message": f'deployments.apps "{name}" not found'}
            if sub == "scale" and method == "GET":
                return 200, {"kind": "Scale", "spec": {"replicas": self.deployments[name]}}
            if sub == "scale" and method == "PATCH":
                if self.forbid_patch:
                    return 403, {"kind": "Status", "message": "forbidden"}
                self.deployments[name] = body["spec"]["replicas"]
                return 200, {"kind": "Scale", "spec": {"replicas": self.deployments[name]}}
            if sub == "" and method == "GET":
                return 200, {"status": {"readyReplicas": self.ready.get(name, 0)}}
        return 404, None


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_ENABLED", "1")
    monkeypatch.setenv("RAY_MODELS_DIR", str(tmp_path / "no-models"))
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(tmp_path / "no-pack"))
    for var in ("EXAMLOPS_K8S_API", "KUBERNETES_SERVICE_HOST", "EXAMLOPS_AUTOSCALE_K8S_TARGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EXAMLOPS_K8S_TOKEN_FILE", str(tmp_path / "missing-token"))
    from examlops import platform_db
    from examlops.coordination import reset_coordinator

    reset_coordinator()
    platform_db.init_db()


def test_reads_and_patches_the_scale_subresource():
    api = FakeApiServer({"jpcp-predictor": 2})
    ap = KubernetesApplier(api, namespace="ml")
    assert ap.current_replicas("JPCP") == 2
    ap.apply("JPCP", 2, 5)
    assert api.deployments["jpcp-predictor"] == 5
    patch = [c for c in api.calls if c[0] == "PATCH"]
    assert patch == [
        (
            "PATCH",
            "/apis/apps/v1/namespaces/ml/deployments/jpcp-predictor/scale",
            {"spec": {"replicas": 5}},
        )
    ]


def test_refuses_when_an_hpa_or_keda_owns_the_deployment():
    api = FakeApiServer(
        {"jpcp-predictor": 2},
        hpas=[("other", "x"), ("y", "z"), ("keda-hpa-jpcp-autoscale", "jpcp-predictor")],
        page=1,  # the owner is on the third page: paging is followed
    )
    ap = KubernetesApplier(api, namespace="ml")
    with pytest.raises(ScaleApplierUnavailable, match="keda-hpa-jpcp-autoscale"):
        ap.apply("JPCP", 2, 4)
    assert api.deployments["jpcp-predictor"] == 2
    assert not [c for c in api.calls if c[0] == "PATCH"]


def test_missing_deployment_forbidden_and_invalid_counts():
    ap = KubernetesApplier(FakeApiServer({}), namespace="ml")
    assert ap.current_replicas("JPCP") is None  # unknown -> the controller holds
    with pytest.raises(ScaleApplyError, match="not found"):
        ap.apply("JPCP", 0, 1)
    ap2 = KubernetesApplier(FakeApiServer({"jpcp-predictor": 1}, forbid_patch=True), namespace="ml")
    with pytest.raises(ScaleApplierUnavailable, match="RBAC"):
        ap2.apply("JPCP", 1, 2)
    with pytest.raises(ScaleApplyError):
        ap2.apply("JPCP", 1, -1)


def test_target_template_and_bad_template():
    api = FakeApiServer({"serve-jpcp": 1})
    ap = KubernetesApplier(api, namespace="ml", target_template="serve-{name}")
    assert ap.current_replicas("JPCP") == 1
    with pytest.raises(K8sConfigError):
        KubernetesApplier(api, namespace="ml", target_template="fixed-name")


def test_controller_drives_the_k8s_applier_and_audits():
    set_policy("JPCP", min_replicas=1, max_replicas=8, target_metric="rps", target_value=10)
    api = FakeApiServer({"jpcp-predictor": 1})

    class Sig:
        def read(self, model, policy):
            return Signals(rps=35)

    rep = AutoscaleController(
        Sig(), KubernetesApplier(api, namespace="ml"), dry_run=False
    ).run_cycle()
    assert rep.results[0].outcome == "applied"
    assert api.deployments["jpcp-predictor"] == 4
    from examlops.data.serving import list_scale_events

    assert list_scale_events("JPCP", last_n=1)[0]["to_replicas"] == 4


def test_transport_fails_closed_on_unsafe_configuration(monkeypatch):
    with pytest.raises(K8sConfigError, match="no Kubernetes API"):
        http_transport()
    monkeypatch.setenv("EXAMLOPS_K8S_API", "http://10.0.0.5:6443")
    with pytest.raises(K8sConfigError, match="plain http"):
        http_transport()
    monkeypatch.setenv("EXAMLOPS_K8S_API", "https://k8s.example:6443")
    with pytest.raises(K8sConfigError, match="token"):
        http_transport()  # no token file -> refuse, never an anonymous request
    monkeypatch.setenv("EXAMLOPS_K8S_API", "ftp://x")
    with pytest.raises(K8sConfigError):
        http_transport()


def test_make_applier_knows_k8s_and_config_error_surfaces(monkeypatch):
    ap = make_applier("k8s")
    assert ap.name == "k8s"
    with pytest.raises(K8sConfigError):
        ap.current_replicas("JPCP")  # misconfiguration is not "unknown replicas"


def test_real_urllib_transport_against_a_loopback_api(monkeypatch):
    api = FakeApiServer({"jpcp-predictor": 3})
    seen_headers: list[dict[str, str]] = []

    class H(BaseHTTPRequestHandler):
        def _serve(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            seen_headers.append(dict(self.headers))
            status, payload = api(self.command, self.path, body)
            raw = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_PATCH = _serve

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setenv("EXAMLOPS_K8S_API", f"http://127.0.0.1:{srv.server_port}")
        ap = KubernetesApplier(http_transport(), namespace="ml")
        assert ap.current_replicas("JPCP") == 3
        ap.apply("JPCP", 3, 0)  # scale to zero
        assert api.deployments["jpcp-predictor"] == 0
        patch_headers = [h for h in seen_headers if h.get("Content-Type")]
        assert patch_headers[-1]["Content-Type"] == "application/merge-patch+json"
    finally:
        srv.shutdown()
