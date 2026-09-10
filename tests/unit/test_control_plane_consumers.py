"""Every consumer's call to the control plane must match the control plane's committed contract.

Plan P0.3 / finding B3 (2026-09-10). The 2026-09-04 audit put ``read``-scope auth on ``/models*``.
The server was right and its contract snapshot was updated — but ``api-contract.json`` only pins the
*server's* shape, and nothing checked what the consumers send. So the dashboard's Models pages, the
Skipper registry/approvals/ModelZoo tools and ``exa cards`` all started getting 401, and
``exa approvals delete`` had been calling a route that never existed, with every suite green.

This guard reads the consumers' source (the CLI package, the dashboard backend, the Skipper agent,
the platform clients) and, for every URL built from the control plane's base address, checks:

1. the route and method exist in ``platform/services/control_plane/api-contract.json``, and
2. a route that needs a credential is called with one — a ``token=`` or ``headers=`` argument, or the
   agent's ``request_json("control_plane", …)``, which attaches the bearer itself.

It is static on purpose: a consumer test with an HTTP double only proves the consumer agrees with
the double. The dashboard's ``ControlPlaneClient`` builds relative paths, so it is checked on its
own terms: its paths must exist, and every production construction must pass a ``token_provider``.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "platform" / "services" / "control_plane" / "api-contract.json"
DASHBOARD = ROOT / "platform" / "services" / "dashboard" / "backend"
SCANNED = [
    ROOT / "platform" / "cli" / "src" / "examlops",
    DASHBOARD,
    ROOT / "platform" / "services" / "agent" / "skipper",
    ROOT / "platform" / "clients",
]

# Routes a caller may reach with no credential: the probes, the scrape endpoint and the webhooks
# (which authenticate with their own shared secret / HMAC, not a bearer).
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/livez"),
    ("GET", "/readyz"),
    ("GET", "/metrics"),
    ("POST", "/webhooks/modelzoo/gitlab"),
    ("POST", "/webhooks/modelzoo/github"),
}

_HTTP_VERBS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
# Wrappers named for what they do rather than the verb (`_safe_get`, the MCP tools' `_get`).
_VERB_ALIASES = {"_get": "GET", "_safe_get": "GET", "_post": "POST"}
_BASE = re.compile(r"(?<!public_)control_plane_url$", re.IGNORECASE)


@dataclass(frozen=True)
class CallSite:
    where: str
    method: str | None
    path: str
    credentialed: bool


APP = ROOT / "platform" / "services" / "control_plane" / "app.py"
_ROUTE_DECORATOR = re.compile(r'@app\.(get|post|put|delete|patch)\(\s*"(/[^"]*)"')


def _contract() -> dict[str, set[str]]:
    """Every route the control plane serves: the committed contract, plus the probe routes the app
    keeps out of its OpenAPI schema (``include_in_schema=False``), read from its decorators."""
    paths = json.loads(CONTRACT.read_text(encoding="utf-8"))["paths"]
    routes = {_template(p): {m.upper() for m in ops} for p, ops in paths.items()}
    for method, path in _ROUTE_DECORATOR.findall(APP.read_text(encoding="utf-8")):
        routes.setdefault(_template(path), set()).add(method.upper())
    return routes


def _template(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _control_plane_path(node: ast.AST) -> str | None:
    """The route an f-string builds on the control plane's base URL, or None."""
    if not (isinstance(node, ast.JoinedStr) and node.values):
        return None
    head = node.values[0]
    if not (isinstance(head, ast.FormattedValue) and _BASE.search(_dotted(head.value))):
        return None
    parts = [
        v.value if isinstance(v, ast.Constant) else "{}"
        for v in node.values[1:]
        if isinstance(v, ast.Constant | ast.FormattedValue)
    ]
    path = "".join(str(p) for p in parts).split("?", 1)[0]
    return path or "/"


def _classify(call: ast.Call) -> tuple[str | None, bool]:
    """(HTTP method, carries a credential) for a call that sends a control-plane URL."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    keywords = {kw.arg for kw in call.keywords}
    if name == "request_json":
        service = call.args[0] if call.args else None
        method = call.args[1] if len(call.args) > 1 else None
        verb = method.value if isinstance(method, ast.Constant) else None
        auto = isinstance(service, ast.Constant) and service.value == "control_plane"
        return verb, auto or "headers" in keywords
    verb = _HTTP_VERBS.get(name) or _VERB_ALIASES.get(name)
    if verb is None and name == "request" and call.args and isinstance(call.args[0], ast.Constant):
        verb = str(call.args[0].value).upper()
    return verb, bool(keywords & {"token", "headers"})


def _calls_using(scope: ast.AST, var: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(scope)
        if isinstance(n, ast.Call)
        and any(
            isinstance(a, ast.Name) and a.id == var
            for a in [*n.args, *[k.value for k in n.keywords]]
        )
    ]


def _call_sites() -> list[CallSite]:
    sites: list[CallSite] = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.relative_to(ROOT).parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parents = {
                child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
            }
            for node in ast.walk(tree):
                route = _control_plane_path(node)
                if route is None:
                    continue
                where = f"{path.relative_to(ROOT)}:{node.lineno}"
                parent = parents.get(node)
                if isinstance(parent, ast.Call):
                    method, credentialed = _classify(parent)
                    sites.append(CallSite(where, method, route, credentialed))
                    continue
                if isinstance(parent, ast.Assign) and isinstance(parent.targets[0], ast.Name):
                    scope = parent
                    while scope in parents and not isinstance(
                        scope, ast.FunctionDef | ast.AsyncFunctionDef
                    ):
                        scope = parents[scope]
                    uses = _calls_using(scope, parent.targets[0].id)
                    if uses:
                        for call in uses:
                            method, cred = _classify(call)
                            sites.append(CallSite(where, method, route, cred))
                        continue
                # A bare URL (a probe table, a link): no call to inspect; must be a public probe.
                sites.append(CallSite(where, "GET", route, False))
    return sites


def test_the_scan_finds_the_known_consumers():
    """Guard the guard: if the extractor silently stops matching, every assertion is vacuous."""
    wheres = {s.where.split(":")[0] for s in _call_sites()}
    for expected in (
        "platform/cli/src/examlops/cli/commands/approvals.py",
        "platform/services/dashboard/backend/routers/approvals.py",
        "platform/services/agent/skipper/tools/registry.py",
        "platform/clients/seanerbus_bridge.py",
    ):
        assert expected in wheres, f"extractor no longer sees {expected}"
    assert len(_call_sites()) >= 40


def test_every_consumer_route_exists_in_the_contract():
    contract = _contract()
    problems = []
    for site in _call_sites():
        methods = contract.get(_template(site.path))
        if methods is None:
            problems.append(f"{site.where}: {site.path} is not a control-plane route")
        elif site.method and site.method not in methods:
            problems.append(
                f"{site.where}: {site.method} {site.path} (route allows {sorted(methods)})"
            )
    assert not problems, "\n".join(problems)


def test_every_protected_route_is_called_with_a_credential():
    problems = [
        f"{s.where}: {s.method} {s.path} is sent without a credential"
        for s in _call_sites()
        if (s.method or "GET", _template(s.path)) not in PUBLIC and not s.credentialed
    ]
    assert not problems, "\n".join(problems)


def test_public_routes_still_exist():
    contract = _contract()
    stale = [f"{m} {p}" for m, p in PUBLIC if m not in contract.get(p, set())]
    assert not stale, f"PUBLIC names routes the control plane no longer has: {stale}"


# ─── the dashboard's ControlPlaneClient (relative paths, credential via token_provider) ───────


def _dashboard_client_paths() -> list[str]:
    tree = ast.parse((DASHBOARD / "control_plane_client.py").read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_get"
            and node.args
        ):
            arg = node.args[0]
            if isinstance(arg, ast.Constant):
                out.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                out.append(
                    "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in arg.values)
                )
    return out


def test_dashboard_client_paths_exist_and_are_sent_with_its_credential():
    contract = _contract()
    paths = _dashboard_client_paths()
    assert paths, "ControlPlaneClient._get call sites not found"
    for path in paths:
        assert "GET" in contract.get(_template(path), set()), f"dashboard client: GET {path}"
    source = (DASHBOARD / "control_plane_client.py").read_text(encoding="utf-8")
    assert "headers=headers" in source and "_auth_headers()" in source


def test_dashboard_builds_its_control_plane_client_with_a_credential():
    unauthenticated = []
    for path in sorted(DASHBOARD.rglob("*.py")):
        if "tests" in path.relative_to(ROOT).parts or path.name == "control_plane_client.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "ControlPlaneClient"
                and "token_provider" not in {kw.arg for kw in node.keywords}
            ):
                unauthenticated.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not unauthenticated, (
        f"ControlPlaneClient built without token_provider: {unauthenticated}"
    )
