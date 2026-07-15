"""
Smoke-test publisher for the seanerbus bridge.

Publishes 3 synthetic HpcJobV1 messages to the given job topic UUID.

Usage:
    python clients/seanerbus_test_pub.py <JOB_TOPIC_UUID>
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import uuid

import capnp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seanerbus_client import Connection  # noqa: E402
from seanerbus_msgs import HpcJobV1  # noqa: E402

SEANERBUS_HOST = os.getenv("SEANERBUS_HOST", "localhost")
SEANERBUS_PORT = int(os.getenv("SEANERBUS_PORT", "5398"))
EMBEDDING_DIM = 384
_USERS = list(range(1, 101))
_NODE_COUNTS = [1, 2, 4, 8, 16]


def _make_job(job_num: int) -> HpcJobV1:
    return HpcJobV1(
        job_id=str(job_num),
        user_id=random.choice(_USERS),
        num_nodes=random.choice(_NODE_COUNTS),
        embedding=[random.uniform(0.0, 1.0) for _ in range(EMBEDDING_DIM)],
    )


async def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <JOB_TOPIC_UUID>")
        sys.exit(1)

    try:
        topic_uuid = uuid.UUID(sys.argv[1])
    except ValueError:
        print(f"ERROR: {sys.argv[1]!r} is not a valid UUID")
        sys.exit(1)

    conn = Connection(SEANERBUS_HOST, SEANERBUS_PORT)
    await conn.connect()

    for i in range(1, 4):
        job = _make_job(i)
        await conn.publish(topic_uuid, job)
        print(f"Published job {job.job_id} (nodes={job.num_nodes}, user={job.user_id})")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(capnp.run(main()))
