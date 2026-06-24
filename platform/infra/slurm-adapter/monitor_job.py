#!/usr/bin/env python
"""
CLI: monitor a submitted Slurm (or mock) job.

Examples
--------
# Check status once:
  python monitor_job.py --job-id <job_id>

# Poll until completion:
  python monitor_job.py --job-id <job_id> --wait

# Print logs after completion:
  python monitor_job.py --job-id <job_id> --logs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adapter import JobNotFoundError, get_slurm_adapter  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Monitor a Slurm (or mock) job")
    p.add_argument("--job-id", required=True, dest="job_id", help="Job ID to monitor")
    p.add_argument(
        "--wait", action="store_true",
        help="Poll until the job reaches a terminal state",
    )
    p.add_argument(
        "--logs", action="store_true",
        help="Print job stdout after checking status",
    )
    p.add_argument(
        "--interval", type=int, default=10,
        help="Polling interval in seconds (default: 10)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    adapter = get_slurm_adapter()

    if args.wait:
        artifact = adapter.wait_until_complete(args.job_id, poll_interval=args.interval)
        print(f"artifact={artifact}")
    else:
        try:
            status = adapter.get_job_status(args.job_id)
        except JobNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"state={status['state']}")
        if status.get("exit_code") is not None:
            print(f"exit_code={status['exit_code']}")
        if status.get("start_time"):
            print(f"start_time={status['start_time']}")
        if status.get("end_time"):
            print(f"end_time={status['end_time']}")

    if args.logs:
        print("\n--- logs ---")
        print(adapter.get_job_logs(args.job_id))


if __name__ == "__main__":
    main()
