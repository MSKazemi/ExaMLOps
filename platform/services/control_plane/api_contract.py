"""The control plane's public API contract, reduced to what consumers depend on.

A raw ``app.openapi()`` snapshot is a guard on the schema *generator*, not the API: fastapi
0.115 and 0.141 emit different documents (OpenAPI 3.0 vs 3.1, model-representation changes)
for an identical service, so committing one would fail CI's fresh-install venv against a
developer's venv with neither having changed the interface. This module reduces the spec to
the surface a consumer can actually break on — routes, methods, parameters, request-body
presence, response codes — which is stable across generator versions.

``make openapi-export`` runs this file to regenerate the committed ``api-contract.json``;
``tests/test_openapi_contract.py`` fails when the live app drifts from it.
"""

from __future__ import annotations

import json
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parent / "api-contract.json"

_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


def reduce_spec(spec: dict) -> dict:
    """Reduce a full OpenAPI document to the version-stable consumer contract."""
    paths = {}
    for path, ops in sorted(spec.get("paths", {}).items()):
        reduced_ops = {}
        for method in _METHODS:
            if method not in ops:
                continue
            op = ops[method]
            reduced_ops[method] = {
                "parameters": sorted(
                    [p["in"], p["name"], bool(p.get("required", False))]
                    for p in op.get("parameters", [])
                ),
                "request_body_required": bool(op.get("requestBody", {}).get("required", False))
                if "requestBody" in op
                else None,
                "responses": sorted(op.get("responses", {})),
            }
        paths[path] = reduced_ops
    return {"title": spec.get("info", {}).get("title", ""), "paths": paths}


def export() -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import app as app_mod

    contract = reduce_spec(app_mod.app.openapi())
    CONTRACT_PATH.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    export()
