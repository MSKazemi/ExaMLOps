#!/usr/bin/env python
"""
Standalone HPC training script for ExaMLOps.

Submitted to Slurm by slurm_submit_task when EXAMLOPS_SLURM_MODE=slurm.
Runs on an HPC compute node (repo on shared filesystem), trains the model,
and saves the estimator to --output so result_fetch_task can load it.

Usage (called automatically by the pipeline — not meant for manual use):
    python pipelines/slurm_train_script.py \\
        --model JPCP \\
        --dataset FDataDataset \\
        --output /shared/slurm_jobs/<job_id>/model.pkl \\
        --mlflow-uri http://mlflow:5000

The script exits 0 on success, non-zero on failure.
Slurm stdout/stderr are captured to the job log files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ── sys.path setup (repo root + modelzoo must be importable on compute node) ──
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_MODELZOO = _REPO_ROOT / "modelzoo"
for _p in (str(_REPO_ROOT), str(_MODELZOO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ExaMLOps HPC training job")
    p.add_argument("--model",       required=True, help="Model class name (e.g. JPCP)")
    p.add_argument("--dataset",     required=True, help="Dataset class name (e.g. FDataDataset)")
    p.add_argument("--output",      required=True, help="Path to save trained estimator (.pkl)")
    p.add_argument("--mlflow-uri",  default="http://localhost:15000", dest="mlflow_uri")
    p.add_argument("--dummy",       action="store_true", help="Use dummy data (no Zenodo download)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    import os
    os.environ["MLFLOW_TRACKING_URI"] = args.mlflow_uri

    print(f"[slurm_train] model={args.model}  dataset={args.dataset}  dummy={args.dummy}")

    # Import pipeline registry (triggers auto-discovery)
    from pipelines.pipeline_generator import MODEL_REGISTRY, _resolve_dataset_cls  # noqa: PLC0415

    if args.model not in MODEL_REGISTRY:
        print(f"[slurm_train] ERROR: '{args.model}' not in MODEL_REGISTRY. "
              f"Available: {list(MODEL_REGISTRY)}", file=sys.stderr)
        sys.exit(1)

    _, config_cls, _ = MODEL_REGISTRY[args.model]
    ds_cls = _resolve_dataset_cls(config_cls, args.dataset)
    model, _, train_loader = config_cls.get_train_components(
        ds_cls, split="train", is_dummy=args.dummy
    )

    print("[slurm_train] Starting training...")
    model.train_step(train_loader)
    print("[slurm_train] Training complete.")

    import joblib  # noqa: PLC0415
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model.estimator, output)
    print(f"[slurm_train] Estimator saved → {output}")


if __name__ == "__main__":
    main()
