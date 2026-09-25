# tests/unit/test_kserve_verify_before_load.py
"""ADR 0142 d3 / spec-usar-1 R-SUB-20 — verify-before-load runs in the KServe pod.

Two halves, both hermetic:

* the **render** — an ``InferenceService`` names the platform's ``ClusterStorageContainer`` and
  carries the per-model inputs as predictor annotations (the downward API reads them); an
  ``LLMInferenceService`` overrides its ``storage-initializer`` image and env. Every wired render
  still validates against the pinned KServe v0.20.0 schemas, and ``enforce`` refuses to render
  what nothing in the pod could verify;
* the **pod entrypoint** (:mod:`examlops.supplychain.pod_verifier`) — download, then a real
  Ed25519 verification of the downloaded bytes; its exit status is what stops the pod.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.serving.substrates import k8s_schema, registry  # noqa: E402
from examlops.serving.substrates.base import RenderError  # noqa: E402
from examlops.serving.substrates.resolve import ResolvedRef  # noqa: E402
from examlops.serving.substrates.verifier import (  # noqa: E402
    ANN_MODE,
    ANN_MODEL,
    ANN_SIGNATURE,
    ANN_VERSION,
    VerifierSpec,
    attach_verifier,
    render_storage_container,
)
from examlops.supplychain import pod_verifier  # noqa: E402

IMAGE = "ghcr.io/example/verified-storage@sha256:" + "a" * 64
RECORD = json.dumps({"algo": "ed25519-v2", "digest": "sha256:" + "b" * 64, "signature": "c2ln"})
PRED = {"name": "JPCP", "framework": "sklearn"}
GEN = {"name": "chat", "task_type": "text_generation", "engine": {"engine": "vllm"}}
PRED_REF = ResolvedRef(
    "jpcp", "17", "Production", "s3://bucket/1/m/artifacts", "sha256:" + "b" * 64, "research",
    RECORD,
)  # fmt: skip
GEN_REF = ResolvedRef("chat", "3", "Production", "hf://Qwen/Qwen2.5-0.5B", "unsigned", "research")
ON = VerifierSpec(mode="enforce", image=IMAGE)
WARN = VerifierSpec(mode="warn", image=IMAGE)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))


# ── render: InferenceService ─────────────────────────────────────────────────


def test_an_isvc_names_the_verifying_storage_container_and_carries_its_inputs():
    rendered = registry.get("kserve", verifier=ON).render(PRED, PRED_REF)
    (isvc,) = rendered.objects
    predictor = isvc["spec"]["predictor"]
    assert predictor["storageContainerName"] == "examlops-verified-storage"
    assert predictor["annotations"] == {
        ANN_MODE: "enforce",
        ANN_MODEL: "jpcp",
        ANN_VERSION: "17",
        ANN_SIGNATURE: RECORD,
    }
    assert k8s_schema.validate(isvc) == []


def test_an_isvc_canary_checks_the_canary_against_its_own_record():
    canary = {"version": "18", "percent": 10, "artifact_uri": "s3://bucket/1/m2/artifacts",
              "signature": '{"digest":"x","signature":"y"}'}  # fmt: skip
    (isvc,) = (
        registry.get("kserve", verifier=ON)
        .render({**PRED, "rollout": {"canary": canary}}, PRED_REF)
        .objects
    )
    stable = isvc["spec"]["predictor"]["annotations"]
    canary_ann = isvc["spec"]["canary"][0]["predictor"]["annotations"]
    assert stable[ANN_VERSION] == "17" and canary_ann[ANN_VERSION] == "18"
    assert canary_ann[ANN_SIGNATURE] == canary["signature"] != stable[ANN_SIGNATURE]
    assert isvc["spec"]["canary"][0]["predictor"]["storageContainerName"]
    assert k8s_schema.validate(isvc) == []


# ── render: LLMInferenceService ──────────────────────────────────────────────


def test_an_llmisvc_overrides_the_storage_initializer_image_and_env():
    rendered = registry.get("kserve", verifier=WARN).render(GEN, GEN_REF)
    (obj,) = rendered.objects
    (init,) = obj["spec"]["template"]["initContainers"]
    assert init["name"] == "storage-initializer" and init["image"] == IMAGE
    env = {e["name"]: e.get("value") for e in init["env"]}
    assert env["EXAMLOPS_VERIFY_MODE"] == "warn"
    assert env["EXAMLOPS_VERIFY_MODEL"] == "chat" and env["EXAMLOPS_VERIFY_VERSION"] == "3"
    assert env["EXAMLOPS_VERIFY_RECORD"] == ""  # unsigned is carried as empty, never invented
    trust = next(e for e in init["env"] if e["name"] == "EXAMLOPS_SIGNING_PUBLIC_KEYS")
    assert trust["valueFrom"]["configMapKeyRef"]["optional"] is True
    assert any("no signature on record" in w for w in rendered.warnings)
    assert k8s_schema.validate(obj) == []


def test_an_llmisvc_canary_wires_each_service_to_its_own_version():
    canary = {"version": "4", "percent": 20, "artifact_uri": "hf://Qwen/Qwen2.5-1.5B"}
    stable, canary_obj = (
        registry.get("kserve", verifier=WARN)
        .render({**GEN, "rollout": {"canary": canary}}, GEN_REF)
        .objects
    )
    versions = [
        {e["name"]: e.get("value") for e in o["spec"]["template"]["initContainers"][0]["env"]}[
            "EXAMLOPS_VERIFY_VERSION"
        ]
        for o in (stable, canary_obj)
    ]
    assert versions == ["3", "4"]


# ── the gate refuses what it cannot verify ───────────────────────────────────


def test_enforce_without_a_verifier_image_refuses_to_render():
    with pytest.raises(RenderError, match="EXAMLOPS_KSERVE_VERIFIER_IMAGE"):
        registry.get("kserve", verifier=VerifierSpec(mode="enforce")).render(PRED, PRED_REF)


def test_enforce_refuses_a_scheme_no_storage_initializer_downloads():
    oci = ResolvedRef("jpcp", "17", None, "oci://registry/jpcp:17", "unsigned", "default")
    with pytest.raises(RenderError, match="nothing in the pod could verify"):
        registry.get("kserve", verifier=ON).render(PRED, oci)


def test_warn_without_an_image_renders_unwired_and_says_so():
    rendered = registry.get("kserve", verifier=VerifierSpec(mode="warn")).render(PRED, PRED_REF)
    assert "storageContainerName" not in rendered.objects[0]["spec"]["predictor"]
    assert any("not rendered" in w for w in rendered.warnings)


def test_off_is_explicit_and_leaves_a_warning():
    rendered = registry.get("kserve", verifier=VerifierSpec(mode="off", image=IMAGE)).render(
        PRED, PRED_REF
    )
    assert "storageContainerName" not in rendered.objects[0]["spec"]["predictor"]
    assert any("off" in w for w in rendered.warnings)


def test_an_unknown_mode_is_refused_not_read_as_off(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SERVING_VERIFY", "enfroce")
    with pytest.raises(RenderError, match="not one of"):
        VerifierSpec.from_env()


def test_the_mode_is_the_serving_path_setting(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SERVING_VERIFY", "ENFORCE")
    monkeypatch.setenv("EXAMLOPS_KSERVE_VERIFIER_IMAGE", IMAGE)
    spec = VerifierSpec.from_env()
    assert spec.mode == "enforce" and spec.image == IMAGE
    assert registry.get("kserve")._verifier == spec  # the substrate reads it once, at construction


def test_wiring_is_pure_and_does_not_mutate_the_input():
    from examlops.serving.substrates import kserve

    base = kserve.render(PRED, PRED_REF)
    snapshot = json.dumps(base, sort_keys=True)
    first, _ = attach_verifier(base, PRED_REF, ON)
    second, _ = attach_verifier(base, PRED_REF, ON)
    assert first == second and json.dumps(base, sort_keys=True) == snapshot


# ── the storage container ────────────────────────────────────────────────────


def test_the_storage_container_validates_and_reads_inputs_from_pod_annotations():
    csc = render_storage_container(ON)
    assert k8s_schema.validate(csc) == []
    assert csc["spec"]["workloadType"] == "initContainer"
    prefixes = {f["prefix"] for f in csc["spec"]["supportedUriFormats"]}
    assert {"s3://", "hf://", "https://"} <= prefixes and "oci://" not in prefixes
    refs = {
        e["name"]: e["valueFrom"].get("fieldRef", {}).get("fieldPath")
        for e in csc["spec"]["container"]["env"]
    }
    assert refs["EXAMLOPS_VERIFY_RECORD"] == f"metadata.annotations['{ANN_SIGNATURE}']"
    assert csc["spec"]["container"]["resources"]["limits"]  # bounded


def test_the_storage_container_needs_an_image():
    with pytest.raises(RenderError, match="EXAMLOPS_KSERVE_VERIFIER_IMAGE"):
        render_storage_container(VerifierSpec(mode="enforce"))


def test_an_unknown_field_in_the_storage_container_fails_the_pinned_schema():
    csc = render_storage_container(ON)
    csc["spec"]["container"]["notAField"] = 1
    assert any("unknown field" in e for e in k8s_schema.validate(csc))


# ── the pod entrypoint: download, then a real verification ───────────────────


def _signed_bundle(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A downloaded artifact dir and its real Ed25519 record, with the trust bundle in env."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from examlops.supplychain import (
        ED25519,
        key_id,
        manifest_digest,
        public_key_b64,
        statement,
    )

    src = tmp_path / "src"
    (src / "model").mkdir(parents=True)
    (src / "MLmodel").write_text("flavors: {}\n")
    (src / "model" / "model.pkl").write_bytes(b"\x00weights\x01")
    key = Ed25519PrivateKey.generate()
    paths = [p for p in src.rglob("*") if p.is_file()]
    digest = manifest_digest(paths, root=src)
    record = {
        "algo": ED25519,
        "digest": digest,
        "signature": base64.b64encode(key.sign(statement("jpcp", "17", digest))).decode(),
        "cert": key_id(key.public_key()),
    }
    monkeypatch.setenv("EXAMLOPS_SIGNING_PUBLIC_KEYS", public_key_b64(key.public_key()))
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", "/dev/null")
    return src, json.dumps(record)


def _copy_download(src: Path):
    import shutil

    def download(srcs: list[str], dests: list[str]) -> None:
        assert srcs == ["s3://bucket/1/m/artifacts"]
        shutil.copytree(src, dests[0], dirs_exist_ok=True)

    return download


def _env(mode: str, record: str) -> dict[str, str]:
    return {
        "EXAMLOPS_VERIFY_MODE": mode,
        "EXAMLOPS_VERIFY_MODEL": "jpcp",
        "EXAMLOPS_VERIFY_VERSION": "17",
        "EXAMLOPS_VERIFY_RECORD": record,
    }


def _run(tmp_path, env, download, capsys) -> tuple[int, dict]:
    code = pod_verifier.main(
        ["s3://bucket/1/m/artifacts", str(tmp_path / "mnt")], environ=env, download=download
    )
    line = capsys.readouterr().err.strip().splitlines()[-1]
    return code, json.loads(line)


def test_a_signed_artifact_passes_in_enforce(tmp_path, monkeypatch, capsys):
    src, record = _signed_bundle(tmp_path, monkeypatch)
    code, log = _run(tmp_path, _env("enforce", record), _copy_download(src), capsys)
    assert code == 0
    assert log["event"] == "examlops.verify_before_load"
    assert log["ok"] is True and log["reason"] == "verified" and log["digest"].startswith("sha256:")


def test_a_tampered_artifact_stops_the_pod_in_enforce(tmp_path, monkeypatch, capsys):
    src, record = _signed_bundle(tmp_path, monkeypatch)
    (src / "model" / "model.pkl").write_bytes(b"\x00swapped\x01")  # bytes changed after signing
    code, log = _run(tmp_path, _env("enforce", record), _copy_download(src), capsys)
    assert code == 1 and log["allowed"] is False and log["reason"].startswith("tampered")


def test_a_tampered_artifact_is_logged_but_loaded_in_warn(tmp_path, monkeypatch, capsys):
    src, record = _signed_bundle(tmp_path, monkeypatch)
    (src / "model" / "model.pkl").write_bytes(b"\x00swapped\x01")
    code, log = _run(tmp_path, _env("warn", record), _copy_download(src), capsys)
    assert code == 0 and log["ok"] is False and log["allowed"] is True


def test_an_untrusted_key_is_a_refusal(tmp_path, monkeypatch, capsys):
    src, record = _signed_bundle(tmp_path, monkeypatch)
    monkeypatch.setenv("EXAMLOPS_SIGNING_PUBLIC_KEYS", "")  # trust ConfigMap missing
    code, log = _run(tmp_path, _env("enforce", record), _copy_download(src), capsys)
    assert code == 1 and log["reason"].startswith("untrusted-key")


@pytest.mark.parametrize(
    ("record", "reason"),
    [("", "unsigned"), ("{not json", "unanswerable"), ('{"digest": "x"}', "unanswerable")],
)
def test_an_unanswerable_verification_stops_the_pod_in_enforce(
    tmp_path, monkeypatch, capsys, record, reason
):
    src, _ = _signed_bundle(tmp_path, monkeypatch)
    code, log = _run(tmp_path, _env("enforce", record), _copy_download(src), capsys)
    assert code == 1 and log["reason"].startswith(reason)


def test_an_empty_download_is_unanswerable_not_a_pass(tmp_path, monkeypatch, capsys):
    _, record = _signed_bundle(tmp_path, monkeypatch)
    code, log = _run(tmp_path, _env("enforce", record), lambda s, d: None, capsys)
    assert code == 1 and "nothing was downloaded" in log["reason"]


def test_a_failed_download_exits_non_zero(tmp_path, monkeypatch, capsys):
    def boom(_s, _d):
        raise RuntimeError("403 Forbidden")

    code, log = _run(tmp_path, _env("warn", "{}"), boom, capsys)
    assert code == 1 and "download failed" in log["reason"]


def test_a_mistyped_mode_is_enforce(tmp_path, monkeypatch, capsys):
    src, _ = _signed_bundle(tmp_path, monkeypatch)
    code, log = _run(tmp_path, _env("enfroce", ""), _copy_download(src), capsys)
    assert code == 1 and log["mode"] == "enforce"


def test_a_model_with_no_mode_fails_closed(tmp_path, monkeypatch, capsys):
    src, _ = _signed_bundle(tmp_path, monkeypatch)
    code, log = _run(tmp_path, _env("", ""), _copy_download(src), capsys)
    assert code == 1 and log["mode"] == "enforce"


def test_a_workload_the_platform_did_not_render_is_passed_through(tmp_path, monkeypatch, capsys):
    src, _ = _signed_bundle(tmp_path, monkeypatch)
    code, log = _run(tmp_path, {}, _copy_download(src), capsys)
    assert code == 0 and log["mode"] == "passthrough"
    assert (tmp_path / "mnt" / "MLmodel").is_file()  # it still downloads


def test_bad_argv_is_a_usage_error(capsys):
    assert pod_verifier.main(["only-one"], environ={}, download=lambda s, d: None) == 2


# ── end to end: the record the registry holds is the record the pod checks ───


def test_the_signature_on_record_travels_through_the_render_into_the_pod(
    tmp_path, monkeypatch, capsys
):
    from examlops.data.registry import store_model_signature
    from examlops.platform_db import init_db
    from examlops.serving.substrates.resolve import resolve_ref

    init_db()
    src, record_json = _signed_bundle(tmp_path, monkeypatch)
    record = json.loads(record_json)
    store_model_signature(
        "JPCP",
        "17",
        record["digest"],
        record["signature"],
        algo=record["algo"],
        cert=record["cert"],
        signed_by="operator",
    )
    ref = resolve_ref("JPCP", version="17", artifact_uri="s3://bucket/1/m/artifacts")
    assert ref.signature is not None and "operator" not in ref.signature  # public fields only
    (isvc,) = registry.get("kserve", verifier=ON).render(PRED, ref).objects
    annotations = isvc["spec"]["predictor"]["annotations"]
    # What the downward API hands the storage container: the pod annotations, verbatim.
    env = {
        "EXAMLOPS_VERIFY_MODE": annotations[ANN_MODE],
        "EXAMLOPS_VERIFY_MODEL": annotations[ANN_MODEL],
        "EXAMLOPS_VERIFY_VERSION": annotations[ANN_VERSION],
        "EXAMLOPS_VERIFY_RECORD": annotations[ANN_SIGNATURE],
    }
    code, log = _run(tmp_path, env, _copy_download(src), capsys)
    assert code == 0 and log["reason"] == "verified"


def test_no_record_resolves_to_no_signature():
    from examlops.platform_db import init_db
    from examlops.serving.substrates.resolve import resolve_ref

    init_db()
    ref = resolve_ref("GHOST", version="1", artifact_uri="s3://bucket/x")
    assert ref.signature is None


# ── review fixes: nothing downloaded may escape the check ────────────────────


def test_a_second_downloaded_artifact_is_unanswerable_not_skipped(tmp_path, monkeypatch, capsys):
    """Only the first destination used to be verified: a second pair reached the server unchecked."""
    import shutil

    src, record = _signed_bundle(tmp_path, monkeypatch)

    def download(srcs: list[str], dests: list[str]) -> None:
        for dest in dests:
            shutil.copytree(src, dest, dirs_exist_ok=True)

    code = pod_verifier.main(
        ["s3://b/1/m/artifacts", str(tmp_path / "mnt"), "s3://b/evil", str(tmp_path / "other")],
        environ=_env("enforce", record),
        download=download,
    )
    log = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert code == 1
    assert log["allowed"] is False and log["reason"].startswith("unanswerable")


def test_an_hmac_record_is_unanswerable_without_reaching_for_a_key(tmp_path, monkeypatch, capsys):
    """The pod holds no shared key and must not go looking in a secret store for one."""
    import examlops.secrets as secrets_mod
    import examlops.supplychain as sc

    src, _record = _signed_bundle(tmp_path, monkeypatch)

    def _no(*_a, **_k):
        raise AssertionError("the pod reached for the HMAC signing key")

    monkeypatch.setattr(sc, "_signing_key", _no)
    monkeypatch.setattr(secrets_mod, "get_secret", _no)
    record = json.dumps({"algo": "hmac-sha256", "digest": "d" * 64, "signature": "e" * 64})
    code, log = _run(tmp_path, _env("enforce", record), _copy_download(src), capsys)
    assert code == 1
    assert log["reason"].startswith("unanswerable") and "Ed25519" in log["reason"]


def test_an_hmac_signed_version_warns_at_render():
    hmac_ref = ResolvedRef(
        "jpcp", "17", "Production", "s3://bucket/1/m/artifacts", "d" * 64, "research",
        json.dumps({"algo": "hmac-sha256", "digest": "d" * 64, "signature": "e" * 64}),
    )  # fmt: skip
    rendered = registry.get("kserve", verifier=ON).render(PRED, hmac_ref)
    assert any("hmac-sha256" in w and "Ed25519" in w for w in rendered.warnings)


def test_a_bad_verify_mode_refuses_render_but_not_stop_or_status(monkeypatch):
    """A typo in EXAMLOPS_SERVING_VERIFY must not lock the operator out of stopping a servable."""

    class _Kubectl:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_any_kind(self, name: str) -> None:
            self.deleted.append(name)

        def get_any_kind(self, name: str):
            return None

    monkeypatch.setenv("EXAMLOPS_SERVING_VERIFY", "enforcee")
    kube = _Kubectl()
    sub = registry.get("kserve", kubectl=kube)
    sub.stop("jpcp")
    assert kube.deleted == ["jpcp"]
    assert sub.status("jpcp").state == "UNKNOWN"
    with pytest.raises(RenderError, match="enforcee"):
        sub.render(PRED, PRED_REF)
