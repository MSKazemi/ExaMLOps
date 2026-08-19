"""
Smoke-test req/res client for the seanerbus bridge.

Sends a single synthetic HpcJobV1 to the JPCP inference handler and prints
the decoded HpcInferenceResV1 response as JSON.

UUID is read from pipelines/models/jpcp.yaml (seanerbus_uuid field).
Override with an explicit UUID as the first argument.

Usage:
    python clients/seanerbus_test_req.py [<INFERENCE_UUID>]
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import uuid
from pathlib import Path

import capnp
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seanerbus_client import Connection  # noqa: E402
from seanerbus_msgs import HpcInferenceResV1, HpcJobV1  # noqa: E402

SEANERBUS_HOST = os.getenv("SEANERBUS_HOST", "localhost")
SEANERBUS_PORT = int(os.getenv("SEANERBUS_PORT", "5398"))
EMBEDDING_DIM = 384

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JPCP_YAML = _REPO_ROOT / "pipelines" / "models" / "jpcp.yaml"


def _jpcp_uuid() -> uuid.UUID:
    if len(sys.argv) > 1:
        try:
            return uuid.UUID(sys.argv[1])
        except ValueError:
            print(f"ERROR: {sys.argv[1]!r} is not a valid UUID")
            sys.exit(1)
    with _JPCP_YAML.open() as f:
        data = yaml.safe_load(f)
    return uuid.UUID(data["seanerbus_uuid"])


async def main() -> None:
    inference_uuid = _jpcp_uuid()

    conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
    await conn.connect()

    job = HpcJobV1(
        job_id="1",
        user_id=random.randint(1, 100),
        num_nodes=random.choice([1, 2, 4, 8, 16]),
        embedding=[random.uniform(0.0, 1.0) for _ in range(EMBEDDING_DIM)],
    )

    print(f"Sending HpcJobV1 → {inference_uuid}")
    print(f"  job_id={job.job_id}  user_id={job.user_id}  num_nodes={job.num_nodes}")

    response: HpcInferenceResV1 = await conn.request(inference_uuid, job, HpcInferenceResV1)

    print("\nHpcInferenceResV1 response:")
    print(
        json.dumps(
            {
                "prediction": response.prediction,
                "model_name": response.model_name,
                "model_version": response.model_version,
                "run_id": response.run_id,
                "error_msg": response.error_msg,
            },
            indent=2,
        )
    )

    await conn.close()


if __name__ == "__main__":
    asyncio.run(capnp.run(main()))
