"""Run one submitted reindex inside a scheduler job (ADR 0043 clause 4).

``python -m examlops.embeddings.job --job-id N [--corpus-size K] [--recall R] [--recall-floor F]``

What ``exa embedding reindex --scheduler``'s generated ``run.sh`` executes. It continues reindex
row ``N`` — the one the submitting process opened and marked ``submitted`` — with the recall
inputs the operator gave, so there is one row per reindex and the recall gate is applied where the
work runs. Collection, tenant and target encoder are read from the row, never from arguments.

Only a ``submitted`` row is run: a job replayed after its reindex finished, or pointed at a row
that was never submitted, does nothing (exit 4) rather than switching an index a second time.

Exit codes: 0 the reindex ran (switched, or kept the old index because recall was below the
floor — a verdict, not a failure) · 1 it raised (the row is marked ``failed``) · 2 bad arguments ·
3 no such row · 4 the row is not ``submitted``.
"""

from __future__ import annotations

import argparse
import sys
import traceback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m examlops.embeddings.job")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--corpus-size", type=int, default=0)
    parser.add_argument("--recall", type=float, default=None)
    parser.add_argument("--recall-floor", type=float, default=0.9)
    parser.add_argument("--actor", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    from examlops import data as platform_db
    from examlops import embeddings

    row = next((r for r in platform_db.list_reindex_jobs() if r.get("id") == args.job_id), None)
    if row is None:
        print(f"[reindex-job] no reindex job {args.job_id}", file=sys.stderr)
        return 3
    if row.get("status") != "submitted":
        print(
            f"[reindex-job] job {args.job_id} is {row.get('status')!r}, not 'submitted' — "
            "nothing to run",
            file=sys.stderr,
        )
        return 4
    print(f"[reindex-job] reindexing {row['collection']} → {row['to_encoder']}", flush=True)
    try:
        result = embeddings.reindex(
            row["collection"],
            row["to_encoder"],
            tenant=row.get("tenant") or "default",
            corpus_size=args.corpus_size,
            recall=args.recall,
            recall_floor=args.recall_floor,
            actor=args.actor,
            _resume_job_id=args.job_id,
        )
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        platform_db.update_reindex_job(args.job_id, status="failed")
        return 1
    print(f"[reindex-job] {result.reason}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
