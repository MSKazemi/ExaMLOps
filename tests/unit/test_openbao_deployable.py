"""ADR 0011 clause 1 made true by construction: OpenBao is deployable, opt-in, and not exposed.

The secrets client has talked to OpenBao through ``EXAMLOPS_VAULT_ADDR`` since D7, but no shipped
configuration could run one. Compose now has an ``openbao`` service under the ``secrets`` profile,
the overlay ``docker-compose.secrets.yml`` is the only thing that points services at it, and the
Helm chart deploys it when ``secrets.openbao.enabled``. This guard holds those to what they claim:

* no shipped Compose file publishes an OpenBao port, and the chart routes nothing to it externally;
* the image is pinned by an exact tag and a digest, never ``latest``, and the image's default
  ``-dev`` command (in-memory, well-known root token) is overridden;
* the profile is opt-in and the default stack is not wired to a vault (default unchanged);
* the shipped files hold no secret value (the platform's own scanner, plus token shapes);
* the Helm server configuration is the Compose one, and the chart is silent when disabled.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.unit._compose_yaml import load_compose

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform" / "infra" / "docker-compose"
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"
HCL = COMPOSE / "openbao" / "openbao.hcl"
OVERLAY = COMPOSE / "docker-compose.secrets.yml"
VAULT = re.compile(r"openbao|vault", re.I)
CONSUMERS = {"control-plane", "dashboard", "agent", "dataplane"}
HELM = shutil.which("helm") or shutil.which("helm", path=str(Path.home() / ".local" / "bin"))
needs_helm = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _compose_files() -> list[Path]:
    return sorted(COMPOSE.glob("docker-compose*.yml"))


def _vault_services() -> list[tuple[str, str, dict]]:
    out = []
    for path in _compose_files():
        for name, svc in ((load_compose(path) or {}).get("services") or {}).items():
            if isinstance(svc, dict) and (
                VAULT.search(name) or VAULT.search(str(svc.get("image")))
            ):
                out.append((path.name, name, svc))
    return out


def _openbao() -> dict:
    return load_compose(COMPOSE / "docker-compose.yml")["services"]["openbao"]


def test_the_guard_sees_the_openbao_service():
    assert any(n == "openbao" for _, n, _ in _vault_services())


def test_no_shipped_compose_file_publishes_an_openbao_port():
    bad = [
        f"{f}:{n} publishes {p!r}"
        for f, n, svc in _vault_services()
        for p in (svc.get("ports") or [])
    ]
    assert not bad, "the secrets manager must be reachable only on the internal network: " + str(
        bad
    )


def test_the_image_is_pinned_by_exact_tag_and_digest_never_latest():
    image = _openbao()["image"]
    assert image.startswith("openbao/openbao:"), image
    ref = image.split(":", 1)[1]
    tag, _, digest = ref.partition("@")
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag), f"exact release tag expected, got {tag!r}"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "pin the digest too"
    assert "latest" not in image and "${" not in image, "the pin must not be overridable"


def test_the_default_dev_command_is_overridden_with_the_file_backed_config():
    """The image's default is `server -dev`: in-memory with a well-known root token."""
    command = _openbao()["command"]
    assert command[0] == "server" and "-dev" not in " ".join(command)
    assert any(part == "-config=/openbao/config/openbao.hcl" for part in command)


def test_the_profile_is_opt_in_and_only_the_overlay_wires_services():
    assert _openbao()["profiles"] == ["secrets"]
    for path in _compose_files():
        if path.name == OVERLAY.name:
            continue
        for name, svc in (load_compose(path).get("services") or {}).items():
            if name == "openbao":
                continue
            assert "VAULT" not in str(svc.get("environment", "")), (
                f"{path.name}:{name} is wired to a vault outside the opt-in overlay"
            )
    services = load_compose(OVERLAY)["services"]
    assert set(services) == CONSUMERS
    for name, svc in services.items():
        env = svc["environment"]
        assert set(env) == {"EXAMLOPS_VAULT_ADDR", "EXAMLOPS_VAULT_TOKEN", "EXAMLOPS_VAULT_STRICT"}
        assert env["EXAMLOPS_VAULT_ADDR"] == "${EXAMLOPS_VAULT_ADDR:-http://openbao:8200}"


def test_the_service_is_hardened_and_stateful():
    svc = _openbao()
    assert svc["user"] == "openbao" and svc["user"] not in ("root", "0")
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc.get("healthcheck", {}).get("test")
    assert svc["restart"] == "unless-stopped"
    assert "openbao_data:/openbao/file" in svc["volumes"]
    assert "openbao_data" in load_compose(COMPOSE / "docker-compose.yml")["volumes"]
    assert svc.get("mem_limit")


def test_the_segmented_overlay_puts_it_on_the_control_zone_with_its_consumers():
    seg = load_compose(COMPOSE / "docker-compose.segmented.yml")["services"]
    assert seg["openbao"]["networks"] == ["control"]
    for name in CONSUMERS:
        assert "control" in seg[name]["networks"], f"{name} cannot reach openbao"


def test_the_server_config_stores_on_the_volume_and_starts_uninitialised():
    text = HCL.read_text()
    assert 'path = "/openbao/file"' in text and 'storage "file"' in text
    assert re.search(r"^\s*ui\s*=\s*false", text, re.M)
    assert "dev_root_token" not in text and "token" not in re.sub(r"#.*", "", text).lower()


# --- no secret values in any shipped file -----------------------------------------------------

_TOKEN_SHAPES = re.compile(r"\b(?:hvs|hvb|hvr|s)\.[A-Za-z0-9]{20,}\b")
_SHIPPED = [
    OVERLAY,
    HCL,
    COMPOSE / "docker-compose.yml",
    CHART / "templates" / "openbao.yaml",
    CHART / "values.yaml",
    ROOT / "docs" / "runbooks" / "openbao.md",
    ROOT / "docs" / "guides" / "secrets.md",
]


@pytest.mark.parametrize("path", _SHIPPED, ids=lambda p: p.name)
def test_no_shipped_file_holds_a_secret_value(path: Path):
    from examlops.secrets import scan_text

    text = path.read_text()
    assert not scan_text(text), f"secret-shaped content in {path.name}"
    assert not _TOKEN_SHAPES.search(text), f"vault token shape in {path.name}"


def test_overlay_values_are_interpolations_or_the_internal_address_only():
    for svc in load_compose(OVERLAY)["services"].values():
        for key, value in svc["environment"].items():
            assert str(value).startswith("${"), f"{key} must come from the environment"
            if key == "EXAMLOPS_VAULT_TOKEN":
                assert value == "${EXAMLOPS_VAULT_TOKEN:-}", "no default token, ever"


# --- Helm ------------------------------------------------------------------------------------


def _render(*extra: str) -> list[dict]:
    proc = subprocess.run(
        [
            HELM,
            "template",
            "rel",
            str(CHART),
            "--namespace",
            "mlops",
            "--set",
            "global.imageRegistry=ghcr.io/mskazemi/",
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]


def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text())


def test_helm_values_default_to_disabled_and_pin_the_image():
    ob = _values()["secrets"]["openbao"]
    assert ob["enabled"] is False
    assert re.fullmatch(r"\d+\.\d+\.\d+", ob["image"]["tag"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", ob["image"]["digest"])
    assert ob["image"]["tag"] == _openbao()["image"].split(":")[1].split("@")[0]
    assert ob["image"]["digest"] == _openbao()["image"].split("@")[1]
    assert "token" not in {k for k in ob if k != "tokenSecret"}


@needs_helm
@pytest.mark.parametrize("extra", [(), ("--set", "networkPolicy.enabled=true")])
def test_helm_is_silent_about_openbao_when_disabled(extra):
    docs = _render(*extra)
    assert not [d for d in docs if VAULT.search(yaml.safe_dump(d))]


@needs_helm
def test_helm_enabled_renders_an_internal_hardened_pinned_server():
    docs = _render("--set", "secrets.openbao.enabled=true", "--set", "networkPolicy.enabled=true")
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs if "openbao" in d["metadata"]["name"]}
    assert {("Service", "rel-examlops-openbao"), ("StatefulSet", "rel-examlops-openbao")} <= kinds
    svc = next(
        d for d in docs if d["kind"] == "Service" and d["metadata"]["name"].endswith("openbao")
    )
    assert svc["spec"]["type"] == "ClusterIP"
    assert not [d for d in docs if d["kind"] == "Ingress" and VAULT.search(yaml.safe_dump(d))]
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    pod = sts["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["image"].startswith("openbao/openbao:") and "@sha256:" in c["image"]
    assert "latest" not in c["image"]
    assert c["args"][0] == "server" and "-dev" not in c["args"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert not any("hostPort" in p for p in c["ports"])
    assert sts["spec"]["volumeClaimTemplates"]
    # A NetworkPolicy admits only the tiers that read secrets.
    np = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("openbao")
    )
    peers = {
        p["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
        for r in np["spec"]["ingress"]
        for p in r["from"]
    }
    assert peers == {"control-plane", "dashboard", "agent"}


@needs_helm
def test_helm_enabled_points_the_tiers_at_it_with_an_optional_token_reference():
    docs = _render("--set", "secrets.openbao.enabled=true", "--set", "secrets.openbao.strict=true")
    cfg = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-config")
    )
    assert cfg["data"]["EXAMLOPS_VAULT_ADDR"] == "http://rel-examlops-openbao:8200"
    assert cfg["data"]["EXAMLOPS_VAULT_STRICT"] == "1"
    for tier in ("control-plane", "dashboard", "agent"):
        dep = next(
            d
            for d in docs
            if d["kind"] == "Deployment" and d["metadata"]["name"].endswith(f"-{tier}")
        )
        env = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        ref = env["EXAMLOPS_VAULT_TOKEN"]["valueFrom"]["secretKeyRef"]
        assert ref["optional"] is True and ref["name"] == "examlops-openbao-token"
        assert "value" not in env["EXAMLOPS_VAULT_TOKEN"], "no literal token"


@needs_helm
def test_helm_server_config_is_the_compose_config():
    docs = _render("--set", "secrets.openbao.enabled=true")
    cm = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("openbao")
    )

    def norm(text: str) -> list[str]:
        lines = [re.sub(r"\s+", " ", ln.split("#")[0]).strip() for ln in text.splitlines()]
        return [ln for ln in lines if ln and not ln.startswith("api_addr")]

    assert norm(cm["data"]["openbao.hcl"]) == norm(HCL.read_text())
