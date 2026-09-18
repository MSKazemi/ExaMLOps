"""The control plane's retrain dispatch target must be the deployment the platform creates.

Plan P0.2 / finding B2 (2026-09-10): the control plane dispatched to
`examlops_scheduled_training/nightly`, `exa pipeline deploy` created `…/examlops-nightly`, and that
wrapper flow accepted only `is_dummy` while the control plane sent model/dataset/backend. Each side
was internally consistent and every unit test used a gateway double, so all three facts could be
true at once with a green suite and a platform on which no dispatched retrain ever ran.

This guard reads the source, not a running Prefect, so it holds on a bare CI runner: the target's
name is defined in three places that must agree, and the flow behind it must accept exactly what
the control plane sends.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "pipelines" / "deploy.py"
GENERATOR = ROOT / "pipelines" / "pipeline_generator.py"
CONTROL_PLANE = ROOT / "platform" / "services" / "control_plane" / "app.py"
COMPOSE = ROOT / "platform" / "infra" / "docker-compose" / "docker-compose.yml"
ENV_DOC = ROOT / "docs" / "reference" / "env-vars.md"


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _constant(path: Path, name: str) -> object:
    for node in ast.walk(_module(path)):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not defined in {path.relative_to(ROOT)}")


def _training_flow() -> tuple[str, list[str]]:
    for node in _module(GENERATOR).body:
        if isinstance(node, ast.FunctionDef) and node.name == "training_flow":
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "id", "") == "flow":
                    for kw in dec.keywords:
                        if kw.arg == "name":
                            return ast.literal_eval(kw.value), [a.arg for a in node.args.args]
    raise AssertionError("pipeline_generator.training_flow with @flow(name=...) not found")


def _dispatch_target() -> str:
    flow_name, _ = _training_flow()
    return f"{flow_name}/{_constant(DEPLOY, 'DISPATCH_DEPLOYMENT_NAME')}"


def _control_plane_sends() -> set[str]:
    for node in ast.walk(_module(CONTROL_PLANE)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_DISPATCH_SENDS" for t in node.targets
        ):
            call = node.value
            assert isinstance(call, ast.Call), "_DISPATCH_SENDS must be frozenset({...})"
            return set(ast.literal_eval(call.args[0]))
    raise AssertionError("_DISPATCH_SENDS is not defined in the control plane")


def test_dispatch_deployment_serves_training_flow_itself():
    source = DEPLOY.read_text(encoding="utf-8")
    assert "training_flow.to_deployment(" in source
    assert "name=DISPATCH_DEPLOYMENT_NAME" in source


def test_control_plane_default_is_the_dispatch_deployment():
    match = re.search(
        r'PREFECT_DEPLOYMENT_NAME = os\.getenv\("PREFECT_DEPLOYMENT_NAME", "([^"]+)"\)',
        CONTROL_PLANE.read_text(encoding="utf-8"),
    )
    assert match, "control plane PREFECT_DEPLOYMENT_NAME default not found"
    assert match.group(1) == _dispatch_target()


def test_compose_default_is_the_dispatch_deployment():
    match = re.search(
        r"PREFECT_DEPLOYMENT_NAME:\s*\$\{PREFECT_DEPLOYMENT_NAME:-([^}]+)\}",
        COMPOSE.read_text(encoding="utf-8"),
    )
    assert match, "compose PREFECT_DEPLOYMENT_NAME default not found"
    assert match.group(1) == _dispatch_target()


def test_documented_default_is_the_dispatch_deployment():
    row = next(
        line
        for line in ENV_DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `PREFECT_DEPLOYMENT_NAME` |")
    )
    assert f"`{_dispatch_target()}`" in row


def test_target_flow_accepts_exactly_what_the_control_plane_sends():
    _, flow_params = _training_flow()
    sends = _control_plane_sends()
    assert sends <= set(flow_params), f"flow does not accept {sorted(sends - set(flow_params))}"
    assert set(flow_params) <= sends, (
        f"flow takes {sorted(set(flow_params) - sends)}, which the control plane never sends — "
        "add them to _DISPATCH_SENDS and to what every dispatch path builds"
    )
