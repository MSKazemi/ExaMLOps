"""Reusable httpx MockTransports for control-plane and MLflow."""

from __future__ import annotations

import httpx


def make_control_plane_transport(
    meta_by_model: dict[str, dict],
    readmes: dict[str, tuple[str, str]] | None = None,
) -> httpx.MockTransport:
    """Emulate the control-plane endpoints the dashboard uses."""
    readmes = readmes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/models":
            return httpx.Response(
                200,
                json=[
                    {"model_name": name, "datasets": meta["supported_datasets"]}
                    for name, meta in meta_by_model.items()
                ],
            )
        if path.startswith("/models/") and path.endswith("/meta"):
            name = path.split("/")[2]
            if name not in meta_by_model:
                return httpx.Response(404, json={"detail": "Unknown"})
            return httpx.Response(200, json=meta_by_model[name])
        if path.startswith("/models/") and path.endswith("/readme"):
            name = path.split("/")[2]
            text, sha = readmes.get(name, ("", ""))
            return httpx.Response(200, json={"text": text, "sha": sha})
        return httpx.Response(404, json={"detail": f"unhandled {path}"})

    return httpx.MockTransport(handler)


def make_mlflow_transport(
    versions_by_model: dict[str, list[dict]],
) -> httpx.MockTransport:
    """Emulate MLflow registered-models + model-versions + alias endpoints.

    ``versions_by_model`` keys are model names (matching what the router looks up).
    Each version dict may include an ``"aliases"`` list of alias name strings.
    Alias mutations (POST/DELETE /registered-models/alias) are applied in-memory.
    """
    # Build mutable alias state: {model_name: {alias: version_str}}
    alias_state: dict[str, dict[str, str]] = {}
    for model_name, versions in versions_by_model.items():
        alias_state[model_name] = {}
        for v in versions:
            for alias in v.get("aliases", []):
                alias_state[model_name][alias] = str(v["version"])

    def _resolve_model(name: str) -> tuple[str | None, list[dict]]:
        """Return (canonical_name, versions) — case-insensitive match."""
        if name in versions_by_model:
            return name, versions_by_model[name]
        for k, v in versions_by_model.items():
            if k.lower() == name.lower():
                return k, v
        return None, []

    def _build_aliases_list(canonical: str) -> list[dict]:
        return [{"alias": a, "version": v} for a, v in alias_state.get(canonical, {}).items()]

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        path = request.url.path
        params = request.url.params

        # ── GET /registered-models/get ──────────────────────────────────────
        if "registered-models/get" in path:
            model_name = params.get("name", "")
            canonical, _ = _resolve_model(model_name)
            if canonical is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "registered_model": {
                        "name": model_name,
                        "aliases": _build_aliases_list(canonical),
                    }
                },
            )

        # ── GET/POST/DELETE /registered-models/alias ────────────────────────
        if "registered-models/alias" in path:
            if request.method == "GET":
                model_name = params.get("name", "")
                alias = params.get("alias", "")
                canonical, versions = _resolve_model(model_name)
                if canonical is None:
                    return httpx.Response(404, json={"detail": "Not Found"})
                version = alias_state.get(canonical, {}).get(alias)
                if version is None:
                    return httpx.Response(404, json={"detail": f"Alias {alias!r} not found"})
                mv = next((v for v in versions if str(v["version"]) == version), {})
                return httpx.Response(200, json={"model_version": {**mv, "version": version}})

            if request.method == "POST":
                data = _json.loads(request.content or b"{}")
                model_name = data.get("name", "")
                alias = data.get("alias", "")
                version = str(data.get("version", ""))
                canonical, _ = _resolve_model(model_name)
                if canonical is None:
                    return httpx.Response(404, json={"detail": "Not Found"})
                alias_state.setdefault(canonical, {})[alias] = version
                return httpx.Response(200, json={})

            if request.method == "DELETE":
                data = _json.loads(request.content or b"{}")
                model_name = data.get("name", "")
                alias = data.get("alias", "")
                canonical, _ = _resolve_model(model_name)
                if canonical:
                    alias_state.get(canonical, {}).pop(alias, None)
                return httpx.Response(200, json={})

        # ── GET /model-versions/search ──────────────────────────────────────
        if "model-versions/search" in path or "search-model-versions" in path:
            name = params.get("name") or _extract_name_from_filter(params.get("filter", ""))
            canonical, versions = _resolve_model(name or "")
            return httpx.Response(200, json={"model_versions": versions or []})

        # ── GET /runs/get ───────────────────────────────────────────────────
        if "runs/get" in path:
            run_id = params.get("run_id", "")
            return httpx.Response(
                200,
                json={
                    "run": {
                        "info": {"run_id": run_id},
                        "data": {
                            "metrics": [{"key": "rmse", "value": 42.1}],
                            "tags": [],
                        },
                    }
                },
            )

        return httpx.Response(404, json={"detail": f"unhandled {path}"})

    return httpx.MockTransport(handler)


def _extract_name_from_filter(filter_str: str) -> str:
    """Pull the model name out of a MLflow filter string like ``name='JPCP'``."""
    import re

    m = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", filter_str)
    return m.group(1) if m else ""
