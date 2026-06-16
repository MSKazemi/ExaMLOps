"""SeanerBUS request generator for the DemoAnomaly model.

Synthesizes dummy HPC-job inference requests *in-code* (no external data) and
sends them to the DemoAnomaly inference handler over SeanerBUS req/res, printing
the decoded anomaly prediction for each. Useful for driving the live inference
path of the end-to-end demo and watching predictions / drift accumulate.

A configurable fraction of requests carry *anomalous* embeddings (large-magnitude
outliers) so the detector has something to flag; the rest are drawn from the same
``N(0, 1)`` distribution as the normal training samples.

The model's UUID is read from ``pipelines/models/demoanomaly.yaml`` (override with
``--uuid``).

Usage:
    python platform/clients/seanerbus_demoanomaly_req.py            # 10 reqs @ 2/s
    python platform/clients/seanerbus_demoanomaly_req.py --rate 5 --count 50
    python platform/clients/seanerbus_demoanomaly_req.py --anomaly-frac 0.3
    python platform/clients/seanerbus_demoanomaly_req.py --uuid <UUID>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import capnp
import numpy as np
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seanerbus_client import Connection  # noqa: E402
from seanerbus_msgs import HpcInferenceResV1, HpcJobV1  # noqa: E402

SEANERBUS_HOST = os.getenv("SEANERBUS_HOST", "localhost")
SEANERBUS_PORT = int(os.getenv("SEANERBUS_PORT", "5398"))
EMBEDDING_DIM = 384

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODEL_YAML = _REPO_ROOT / "pipelines" / "models" / "demoanomaly.yaml"


def _model_uuid(explicit: str | None) -> uuid.UUID:
    if explicit:
        return uuid.UUID(explicit)
    with _MODEL_YAML.open() as f:
        data = yaml.safe_load(f)
    return uuid.UUID(data["seanerbus_uuid"])


def _make_embedding(rng: np.random.Generator, anomalous: bool) -> list[float]:
    """Normal: N(0, 1). Anomaly: large-magnitude outlier far from the cluster."""
    if anomalous:
        vec = rng.normal(8.0, 3.0, size=EMBEDDING_DIM)
    else:
        vec = rng.normal(0.0, 1.0, size=EMBEDDING_DIM)
    return [float(x) for x in vec]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DemoAnomaly SeanerBUS request generator")
    p.add_argument("--rate", type=float, default=2.0, help="Requests per second (default: 2)")
    p.add_argument("--count", type=int, default=10, help="Number of requests to send (default: 10)")
    p.add_argument(
        "--anomaly-frac",
        type=float,
        default=0.2,
        help="Fraction of requests that carry anomalous embeddings (default: 0.2)",
    )
    p.add_argument(
        "--alias", default="Production", help="MLflow alias to target (default: Production)"
    )
    p.add_argument("--uuid", default=None, help="Override the model UUID (default: read from YAML)")
    p.add_argument("--seed", type=int, default=7, help="RNG seed (default: 7)")
    return p.parse_args()


async def main() -> None:
    args = _parse_args()
    inference_uuid = _model_uuid(args.uuid)
    rng = np.random.default_rng(args.seed)
    delay = 1.0 / args.rate if args.rate > 0 else 0.0

    conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
    await conn.connect()
    print(
        f"Generating {args.count} request(s) → DemoAnomaly @ {inference_uuid} (rate={args.rate}/s)\n"
    )

    n_anom = 0
    n_flagged = 0
    for i in range(args.count):
        is_anom = rng.random() < args.anomaly_frac
        n_anom += int(is_anom)
        job = HpcJobV1(
            job_id=str(i + 1),
            user_id=int(rng.integers(1, 100)),
            num_nodes=int(rng.choice([1, 2, 4, 8, 16])),
            embedding=_make_embedding(rng, is_anom),
            model_name="DemoAnomaly",
            alias=args.alias,
        )
        try:
            res: HpcInferenceResV1 = await conn.request(inference_uuid, job, HpcInferenceResV1)
        except Exception as exc:  # noqa: BLE001
            print(f"  job {job.job_id}: request failed: {exc}")
            continue

        flagged = int(round(res.prediction)) == 1 if res.error_msg == "" else None
        n_flagged += int(bool(flagged))
        tag = "ANOMALY" if is_anom else "normal "
        verdict = "?" if flagged is None else ("flagged" if flagged else "clean")
        print(
            f"  job {job.job_id:>3} [{tag}] → prediction={res.prediction} "
            f"({verdict})  v={res.model_version}  err={res.error_msg or '-'}"
        )
        if delay and i < args.count - 1:
            await asyncio.sleep(delay)

    print(
        json.dumps(
            {"sent": args.count, "injected_anomalies": n_anom, "flagged_by_model": n_flagged},
            indent=2,
        )
    )
    await conn.close()


if __name__ == "__main__":
    asyncio.run(capnp.run(main()))
