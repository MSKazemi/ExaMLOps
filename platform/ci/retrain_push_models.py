#!/usr/bin/env python3
"""
Trigger retraining for a configured list of models via the Control Plane API.
Called from post-deploy:lxp:retrain-push-models in .gitlab-ci.yml.

Required env vars (set as GitLab CI variables):
  CONTROL_PLANE_URL   — e.g. http://lxp-cpu01:18002
  CONTROL_PLANE_TOKEN — bearer token for /retrain

Optional env vars:
  MODELS          — space-separated model names (default: JPCP MACK MCBound)
  RETRAIN_DATASET — dataset name (default: FDataDataset)
  RETRAIN_DUMMY   — "true" to use dummy data (default: false)
"""
from __future__ import annotations

import json
import os
import sys

import httpx


def main() -> int:
    url = os.environ.get("CONTROL_PLANE_URL", "").rstrip("/")
    token = os.environ.get("CONTROL_PLANE_TOKEN", "")
    models = [m for m in os.environ.get("MODELS", "JPCP MACK MCBound").split() if m]
    dataset = os.environ.get("RETRAIN_DATASET", "FDataDataset")
    is_dummy = os.environ.get("RETRAIN_DUMMY", "false").lower() == "true"

    if not url:
        print("CONTROL_PLANE_URL not set — skipping retrain push", flush=True)
        return 0
    if not token:
        print("CONTROL_PLANE_TOKEN not set — skipping retrain push", flush=True)
        return 0

    headers = {"Authorization": f"Bearer {token}"}
    results: list[dict] = []

    for model in models:
        try:
            r = httpx.post(
                f"{url}/retrain",
                json={"model_name": model, "dataset_name": dataset, "is_dummy": is_dummy},
                headers=headers,
                timeout=15.0,
            )
            r.raise_for_status()
            results.append({"model": model, "status": "triggered", "data": r.json()})
        except Exception as exc:
            results.append({"model": model, "status": "failed", "error": str(exc)})

    for entry in results:
        print(json.dumps(entry), flush=True)

    return 1 if any(e["status"] == "failed" for e in results) else 0


if __name__ == "__main__":
    sys.exit(main())
