"""
Dummy inference client for ExaMLOps Ray Serve.

Usage:
    # Check health and list models, then run built-in example requests:
    python clients/dummy_client.py

    # Send a custom feature dict to a specific model:
    python clients/dummy_client.py --model jpcp --features '{"embedding": [0.1, 0.2, ...]}'

    # Send N random requests and report latency stats:
    python clients/dummy_client.py --benchmark 100

    # Target a non-default host/port:
    python clients/dummy_client.py --url http://localhost:18001
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from typing import Any

try:
    import httpx
except ImportError:
    import subprocess

    subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx", "-q"])
    import httpx


# ── Built-in example features per model name ──────────────────────────────────
#
# PM100-trained JPCP: sklearn pipeline receives (num_nodes_req_cat, user_id_cat)
# FData-trained JPCP: sklearn pipeline receives embedding vector (list of floats)
#
# The model name in MLflow is the `model_id` from get_inference_params(), i.e.
# "jpcp".  If multiple dataset variants are registered separately they will
# appear under different names.
_JPCP_EMBEDDING_DIM = 384

_EMBEDDING_MODELS = {"jpcp", "mack", "mcbound"}

EXAMPLE_FEATURES: dict[str, dict[str, Any]] = {
    name: {"embedding": [random.uniform(0.0, 1.0) for _ in range(_JPCP_EMBEDDING_DIM)]}
    for name in _EMBEDDING_MODELS
}

RANDOM_FEATURE_RANGES: dict[str, dict[str, tuple]] = {
    # embedding vector — each dimension sampled in [0, 1]
    name: {"embedding": (0.0, 1.0)}
    for name in _EMBEDDING_MODELS
}


def _get(client: httpx.Client, path: str) -> dict:
    r = client.get(path)
    r.raise_for_status()
    return r.json()


def _post(client: httpx.Client, path: str, body: dict) -> dict:
    r = client.post(path, json=body)
    r.raise_for_status()
    return r.json()


def check_health(client: httpx.Client) -> None:
    print("── Health ──────────────────────────────────────────────")
    data = _get(client, "/health")
    status = data.get("status", "?")
    n = data.get("models_loaded", 0)
    print(f"  status        : {status}")
    print(f"  models_loaded : {n}")
    for info in data.get("models", []):
        name = info.get("model_name", "?")
        print(
            f"  {name:<20} alias={info.get('alias')}  v{info.get('version')}  run_id={info.get('run_id')}"
        )
    print()


def list_models(client: httpx.Client) -> list[str]:
    print("── Models ──────────────────────────────────────────────")
    models = _get(client, "/models")
    names: list[str] = []
    for m in models:
        print(f"  {m['model_name']:<20} v{m.get('model_version')}  run_id={m.get('run_id')}")
        names.append(m["model_name"])
    if not names:
        print("  (no models loaded — run a training pipeline first)")
    print()
    return names


def predict(client: httpx.Client, model_name: str, features: dict[str, Any]) -> dict:
    return _post(client, f"/predict/{model_name}", {"features": features})


def run_examples(client: httpx.Client, model_names: list[str]) -> None:
    print("── Example predictions ─────────────────────────────────")
    for name in model_names:
        features = EXAMPLE_FEATURES.get(name, {k: 1 for k in ["num_nodes_req_cat", "user_id_cat"]})
        print(f"  POST /predict/{name}")
        print(f"    features  : {features}")
        try:
            t0 = time.perf_counter()
            resp = predict(client, name, features)
            latency_ms = (time.perf_counter() - t0) * 1000
            print(f"    prediction: {resp.get('prediction')}")
            print(f"    version   : {resp.get('model_version')}  run_id={resp.get('run_id')}")
            print(f"    latency   : {latency_ms:.1f} ms")
        except httpx.HTTPStatusError as e:
            print(f"    ERROR {e.response.status_code}: {e.response.text}")
        print()


def benchmark(client: httpx.Client, model_name: str, n: int) -> None:
    print(f"── Benchmark: {n} requests → /predict/{model_name} ─────────")
    ranges = RANDOM_FEATURE_RANGES.get(
        model_name,
        {"num_nodes_req_cat": (1, 64), "user_id_cat": (1, 500)},
    )
    latencies: list[float] = []
    errors = 0
    for _ in range(n):
        features = {
            k: [random.uniform(*v) for _ in range(_JPCP_EMBEDDING_DIM)]
            if k == "embedding"
            else random.randint(*v)
            for k, v in ranges.items()
        }
        try:
            t0 = time.perf_counter()
            predict(client, model_name, features)
            latencies.append((time.perf_counter() - t0) * 1000)
        except Exception:
            errors += 1

    ok = len(latencies)
    print(f"  requests  : {n}  (ok={ok}, errors={errors})")
    if latencies:
        print(
            f"  latency   : min={min(latencies):.1f}ms  "
            f"p50={statistics.median(latencies):.1f}ms  "
            f"p95={sorted(latencies)[int(ok * 0.95)]:.1f}ms  "
            f"max={max(latencies):.1f}ms"
        )
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="ExaMLOps dummy inference client")
    ap.add_argument("--url", default="http://localhost:18001", help="Ray Serve base URL")
    ap.add_argument("--model", help="Model name for --features or --benchmark")
    ap.add_argument("--features", help="JSON feature dict, e.g. '{\"num_nodes_req_cat\": 4}'")
    ap.add_argument(
        "--benchmark", type=int, metavar="N", help="Send N random requests and report latency stats"
    )
    args = ap.parse_args()

    print(f"ExaMLOps dummy client → {args.url}\n")

    with httpx.Client(base_url=args.url, timeout=30) as client:
        try:
            check_health(client)
        except httpx.ConnectError:
            print(f"ERROR: cannot connect to {args.url}")
            print("       Start the server with:  make ray-serving-start")
            sys.exit(1)

        model_names = list_models(client)

        if args.features:
            # Single custom predict
            if not args.model:
                if not model_names:
                    print("No models loaded and --model not specified.")
                    sys.exit(1)
                args.model = model_names[0]
                print(f"--model not specified, defaulting to '{args.model}'\n")
            features = json.loads(args.features)
            print(f"── Custom predict → /predict/{args.model} ──────────────")
            print(f"  features  : {features}")
            try:
                t0 = time.perf_counter()
                resp = predict(client, args.model, features)
                latency_ms = (time.perf_counter() - t0) * 1000
                print(f"  prediction: {resp.get('prediction')}")
                print(f"  version   : {resp.get('model_version')}  run_id={resp.get('run_id')}")
                print(f"  latency   : {latency_ms:.1f} ms")
            except httpx.HTTPStatusError as e:
                print(f"  ERROR {e.response.status_code}: {e.response.text}")
            print()

        elif args.benchmark:
            model = args.model or (model_names[0] if model_names else None)
            if not model:
                print("No models loaded.")
                sys.exit(1)
            benchmark(client, model, args.benchmark)

        else:
            # Default: run built-in examples for every loaded model
            if model_names:
                run_examples(client, model_names)
            else:
                print("No models loaded — train a pipeline first:")
                print("  exa pipeline run --dummy   # dummy data (fast)")
                print(
                    "  exa pipeline run --registry pipelines/model_registry.yaml --env prod  # full run"
                )


if __name__ == "__main__":
    main()
