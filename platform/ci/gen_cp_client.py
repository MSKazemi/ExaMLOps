"""Generate ``examlops.control_plane_api`` — the /v1 client — from the committed API contract.

Plan P1.6. Before this, every caller of the control plane hand-built its URLs: the CLI, the MCP
tools, the SDK and the autopilot each carried their own ``f"{base}/approve/{model}"``. A renamed
route broke them one at a time, in production. The client is now generated from
``platform/services/control_plane/api-contract.json`` — the same file the service's contract test
holds the live app to — so a route change that is not regenerated fails CI on both sides.

Usage::

    python platform/ci/gen_cp_client.py           # write the client
    python platform/ci/gen_cp_client.py --check   # exit 1 if the committed client is stale

Only ``/v1`` operations are generated; every one needs a name in :data:`NAMES`, and the generator
refuses to run when one is missing, so a new /v1 route cannot ship without a client method.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "platform" / "services" / "control_plane" / "api-contract.json"
OUTPUT = REPO / "platform" / "cli" / "src" / "examlops" / "control_plane_api.py"

# (method, path) → client function name. The one hand-written part: names are API design.
NAMES: dict[tuple[str, str], str] = {
    ("get", "/v1/status"): "status",
    ("get", "/v1/models"): "list_models",
    ("get", "/v1/models/{name}/meta"): "model_meta",
    ("get", "/v1/models/{name}/readme"): "model_readme",
    ("get", "/v1/models/{name}/images/{filename}"): "model_image",
    ("get", "/v1/runs/{flow_run_id}"): "run_status",
    ("get", "/v1/approvals"): "list_approvals",
    ("post", "/v1/approvals/{model_id}/approve"): "approve",
    ("post", "/v1/approvals/{model_id}/reject"): "reject",
    ("delete", "/v1/approvals/{approval_id}"): "retract_approval",
    ("post", "/v1/changes"): "report_changes",
    ("get", "/v1/modelzoo/status"): "modelzoo_status",
    ("get", "/v1/modelzoo/events"): "modelzoo_events",
    ("post", "/v1/modelzoo/sync"): "modelzoo_sync",
    ("get", "/v1/modelzoo/config"): "modelzoo_config",
    ("put", "/v1/modelzoo/config"): "set_modelzoo_config",
    ("post", "/v1/admin/reload"): "reload_registry",
    ("post", "/v1/retrain"): "submit_retrain",
    ("get", "/v1/commands"): "list_commands",
    ("get", "/v1/commands/{command_id}"): "get_command",
    ("delete", "/v1/commands/{command_id}"): "cancel_command",
}

_HEADER_ARGS = {"authorization": None, "idempotency-key": "idempotency_key"}
_PARAM = re.compile(r"\{([^}]+)\}")

_HEADER = '''"""The control plane's /v1 API as Python functions — GENERATED, do not edit.

Regenerate with ``make openapi-export`` (or ``python platform/ci/gen_cp_client.py``) after changing a
route; ``tests/unit/test_control_plane_client_generated.py`` fails while this file is stale.

Every function takes ``base`` (the control plane's URL) and ``token`` (a bearer credential) as
keyword arguments, defaulting to the CLI's configuration (``control_plane_url`` /
``control_plane_token``). Calls go through ``examlops.cli._client``, so errors surface as its
``ClientError`` with the server's ``detail`` — for /v1, the RFC 9457 problem document's.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from examlops.cli import _client


def _base_and_token(base: str | None, token: str | None) -> tuple[str, str]:
    if base is None or token is None:
        from examlops.cli._config import load_config

        cfg = load_config()
        base = cfg.control_plane_url if base is None else base
        token = (cfg.control_plane_token or "") if token is None else token
    return base.rstrip("/"), token


def _query(params: dict[str, Any]) -> str:
    present = {k: v for k, v in params.items() if v is not None}
    return f"?{urlencode(present)}" if present else ""


def _seg(value: Any) -> str:
    return quote(str(value), safe="")
'''


def _function(method: str, path: str, op: dict, name: str) -> str:
    path_params = _PARAM.findall(path)
    query = [p[1] for p in op["parameters"] if p[0] == "query"]
    headers = [p[1].lower() for p in op["parameters"] if p[0] == "header"]
    unknown = [h for h in headers if h not in _HEADER_ARGS]
    if unknown:
        raise SystemExit(f"{method.upper()} {path}: no client mapping for header(s) {unknown}")
    has_body = op["request_body_required"] is not None
    args = [f"{p}: str" for p in path_params]
    kwargs = []
    if has_body:
        kwargs.append("body: dict[str, Any] | None = None")
    kwargs += [f"{q}: Any = None" for q in sorted(query)]
    if "idempotency-key" in headers:
        kwargs.append("idempotency_key: str | None = None")
    if method == "post":
        kwargs.append("timeout: float | None = None")
    kwargs += ["base: str | None = None", "token: str | None = None"]
    signature = ", ".join([*args, "*", *kwargs])
    url_path = _PARAM.sub(lambda m: "{_seg(" + m.group(1) + ")}", path)
    query_expr = (
        " + _query({" + ", ".join(f'"{q}": {q}' for q in sorted(query)) + "})" if query else ""
    )
    lines = [
        f"def {name}({signature}) -> Any:",
        f'    """``{method.upper()} {path}``."""',
        "    base, token = _base_and_token(base, token)",
        f'    url = f"{{base}}{url_path}"{query_expr}',
    ]
    if method == "get":
        lines.append("    return _client.get(url, token=token)")
    elif method == "delete":
        lines.append("    return _client.delete(url, token=token)")
    elif method == "put":
        lines.append("    return _client.put(url, body or {}, token=token)")
    elif method == "post":
        extra = ", idempotency_key=idempotency_key" if "idempotency-key" in headers else ""
        payload = "body or {}" if has_body else "{}"
        # Two calls rather than **kwargs: the type checker can see both, and a caller that passes
        # no timeout keeps `_client.post`'s own default.
        lines.append("    if timeout is None:")
        lines.append(f"        return _client.post(url, {payload}, token=token{extra})")
        lines.append(f"    return _client.post(url, {payload}, token=token, timeout=timeout{extra})")
    else:
        raise SystemExit(f"unsupported method {method}")
    return "\n".join(lines)


def render(contract: dict) -> str:
    ops = [
        (method, path, op)
        for path, methods in sorted(contract["paths"].items())
        if path.startswith("/v1/")
        for method, op in sorted(methods.items())
    ]
    missing = [f"{m.upper()} {p}" for m, p, _ in ops if (m, p) not in NAMES]
    if missing:
        raise SystemExit(f"name these /v1 operations in gen_cp_client.NAMES: {missing}")
    stale = [f"{m.upper()} {p}" for (m, p) in NAMES if p not in contract["paths"]]
    if stale:
        raise SystemExit(f"NAMES lists operations the contract no longer has: {stale}")
    body = "\n\n\n".join(_function(m, p, op, NAMES[(m, p)]) for m, p, op in ops)
    names = sorted(NAMES[(m, p)] for m, p, _ in ops)
    exports = "__all__ = [\n" + "".join(f'    "{n}",\n' for n in names) + "]\n"
    return _format(f"{_HEADER}\n\n{exports}\n\n{body}\n")


def _format(source: str) -> str:
    """The repo's formatter, so the generated file passes `ruff format --check` like any other."""
    import subprocess

    done = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename", str(OUTPUT), "-"],
        input=source,
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    return done.stdout


def main(argv: list[str]) -> int:
    rendered = render(json.loads(CONTRACT.read_text(encoding="utf-8")))
    if "--check" in argv:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT.relative_to(REPO)} is stale — run: python platform/ci/gen_cp_client.py")
            return 1
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
