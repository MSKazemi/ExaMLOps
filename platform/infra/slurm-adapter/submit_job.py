#!/usr/bin/env python
"""
CLI: submit a training job via the ExaMLOps Slurm adapter.

Examples
--------
# Mock mode (default — no Slurm required):
  python submit_job.py --script train.sh

# Real Slurm:
  EXAMLOPS_SLURM_MODE=slurm python submit_job.py \\
      --script train.sh \\
      --partition gpu \\
      --nodes 2 \\
      --time 4:00:00 \\
      --mem 32G \\
      --cpus-per-task 8

# Wait for completion and print logs:
  python submit_job.py --script train.sh --wait
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adapter import get_slurm_adapter  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Submit a Slurm (or mock) training job")
    p.add_argument("--script", required=True, help="Path to the job script (.sh or .py)")
    p.add_argument("--partition",     default=None, help="Slurm partition")
    p.add_argument("--nodes",         default=None, help="Number of nodes")
    p.add_argument("--ntasks",        default=None, help="Number of tasks")
    p.add_argument("--cpus-per-task", default=None, dest="cpus_per_task", help="CPUs per task")
    p.add_argument("--mem",           default=None, help="Memory per node (e.g. 16G)")
    p.add_argument("--gpus",          default=None, help="GPUs (e.g. 1 or a100:2)")
    p.add_argument("--time",          default=None, help="Wall-clock limit (e.g. 2:00:00)")
    p.add_argument("--job-name",      default=None, dest="job_name", help="Job name")
    p.add_argument("--account",       default=None, help="Slurm account/project")
    p.add_argument(
        "--wait", action="store_true",
        help="Block until job completes and print the log",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    resources = {
        k: v for k, v in {
            "partition":     args.partition,
            "nodes":         args.nodes,
            "ntasks":        args.ntasks,
            "cpus_per_task": args.cpus_per_task,
            "mem":           args.mem,
            "gpus":          args.gpus,
            "time":          args.time,
            "job_name":      args.job_name,
            "account":       args.account,
        }.items()
        if v is not None
    }

    adapter = get_slurm_adapter()
    job_id = adapter.submit_job(script_path=args.script, resources=resources or None)
    print(f"job_id={job_id}")

    if args.wait:
        artifact = adapter.wait_until_complete(job_id)
        print(f"artifact={artifact}")
        print("\n--- logs ---")
        print(adapter.get_job_logs(job_id))


if __name__ == "__main__":
    main()
