# tests/unit/test_kuberay_render.py
"""ADR 0015 d1/d5 — the Ray multi-model server on Kubernetes as a KubeRay ``RayService``.

Validated offline against the vendored KubeRay v1.7.1 CRD schema (the same structural walker as
the KServe renders), carrying the Compose path's observability and verify-before-load settings,
and never a credential.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.serving.substrates import k8s_schema, kuberay  # noqa: E402
from examlops.serving.substrates.resolve import RenderError  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/example/examlops-ray-serving:v1.2.3"
# Built at runtime so no credential-shaped literal sits in the source (gitleaks).
FAKE_SECRET = "".join(["not", "-a-", "real", "-value"])
FAKE_TOKEN = "".join(["fake", "-admin"])


def _render(**kw):
    return kuberay.render_ray_service(["JPCP", "MACK"], image=IMAGE, **kw)


def test_a_rayservice_validates_against_the_pinned_kuberay_schema():
    obj = _render()
    assert obj["apiVersion"] == "ray.io/v1" and obj["kind"] == "RayService"
    assert k8s_schema.validate(obj) == []
    assert ("RayService", "ray.io/v1") in k8s_schema.pinned_kinds()


def test_the_schema_catches_an_unknown_field():
    obj = _render()
    obj["spec"]["rayClusterConfig"]["headGroupSpec"]["notAField"] = True
    assert any("unknown field" in e for e in k8s_schema.validate(obj))


def test_it_serves_the_same_applications_as_compose():
    serve = yaml.safe_load(_render()["spec"]["serveConfigV2"])
    routes = {a["route_prefix"]: a["import_path"] for a in serve["applications"]}
    assert routes == {
        "/": "serving.ray_serving.app:build_app",
        "/infer-pipeline": "serving.inference_pipeline.app:pipeline_app",
    }


def test_the_multi_model_import_path_exists_in_the_serving_code():
    """The builder the serve config names must exist — a typo would only fail in the cluster."""
    source = (REPO / "serving" / "ray_serving" / "app.py").read_text()
    assert re.search(r"^def build_app\(", source, re.MULTILINE)
    pipeline = (REPO / "serving" / "inference_pipeline" / "app.py").read_text()
    assert re.search(r"^pipeline_app\s*=", pipeline, re.MULTILINE)


def test_the_ray_version_matches_the_serving_image_pin():
    req = (REPO / "serving" / "ray_serving" / "requirements.txt").read_text()
    pinned = re.search(r"^ray\[serve\]==([\d.]+)", req, re.MULTILINE)
    assert pinned and pinned.group(1) == kuberay.RAY_VERSION


def test_observability_continuity_is_rendered():
    obj = _render(env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://tempo:4317"})
    head = obj["spec"]["rayClusterConfig"]["headGroupSpec"]
    worker = obj["spec"]["rayClusterConfig"]["workerGroupSpecs"][0]
    for group in (head, worker):
        assert group["rayStartParams"]["metrics-export-port"] == "8080"
        assert group["template"]["metadata"]["annotations"]["prometheus.io/scrape"] == "true"
    env = {e["name"]: e["value"] for e in head["template"]["spec"]["containers"][0]["env"]}
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://tempo:4317"


def test_otel_sdk_disabled_is_never_rendered_because_it_blinds_ray_metrics():
    obj = _render(env={"OTEL_SDK_DISABLED": "true"})
    names = {
        e["name"]
        for e in obj["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]["containers"][
            0
        ]["env"]
    }
    assert "OTEL_SDK_DISABLED" not in names


def test_verify_before_load_mode_reaches_the_server():
    obj = _render(env={"EXAMLOPS_SERVING_VERIFY": "enforce"})
    env = obj["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]["containers"][0][
        "env"
    ]
    assert {"name": "EXAMLOPS_SERVING_VERIFY", "value": "enforce"} in env


def test_no_credential_from_the_environment_is_rendered():
    obj = _render(
        env={
            "AWS_SECRET_ACCESS_KEY": FAKE_SECRET,
            "RAY_SERVE_ADMIN_TOKEN": FAKE_TOKEN,
            "MLFLOW_TRACKING_URI": "http://mlflow:5000",
        }
    )
    text = yaml.safe_dump(obj)
    assert FAKE_SECRET not in text and FAKE_TOKEN not in text
    assert "RAY_SERVE_ADMIN_TOKEN" not in text and "http://mlflow:5000" in text
    container = obj["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]["containers"][
        0
    ]
    assert container["envFrom"] == [
        {"secretRef": {"name": "examlops-serving-env", "optional": True}}
    ]


def test_the_object_names_what_it_was_planned_to_serve():
    assert _render()["metadata"]["annotations"]["examlops.io/models"] == "JPCP,MACK"


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"image": "ray-serving"}, "pinned"),
        ({"image": ""}, "pinned"),
        ({"name": "Bad_Name"}, "DNS-1035"),
        ({"min_workers": 3, "max_workers": 2}, "min ≤ max"),
        ({"max_workers": 1000}, "256"),
    ],
)
def test_bad_inputs_are_refused(kw, match):
    args = {"image": IMAGE, **kw}
    with pytest.raises(RenderError, match=match):
        kuberay.render_ray_service(["JPCP"], **args)


def test_an_empty_registry_is_refused():
    with pytest.raises(RenderError, match="no models"):
        kuberay.render_ray_service([], image=IMAGE)


def test_registry_to_kuberay_reads_the_pack(tmp_path):
    from examlops.serving_backends import registry_to_kuberay, select_backend

    (tmp_path / "jpcp.yaml").write_text("name: JPCP\nframework: sklearn\n")
    (tmp_path / "mack.yaml").write_text("name: MACK\n")
    obj = registry_to_kuberay(str(tmp_path), image=IMAGE, env={})
    assert obj["metadata"]["annotations"]["examlops.io/models"] == "JPCP,MACK"
    assert select_backend("kuberay-k8s").name == "kuberay-k8s"


def test_a_digest_pinned_image_is_accepted():
    obj = kuberay.render_ray_service(["JPCP"], image="ghcr.io/x/ray@sha256:" + "a" * 64)
    assert k8s_schema.validate(obj) == []


def test_the_kuberay_pin_is_recorded():
    import json

    pin = json.loads((k8s_schema.kuberay_pin_dir() / "PIN.json").read_text())
    assert pin["version"] == k8s_schema.kuberay_pin_dir().name
