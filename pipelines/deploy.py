"""
ExaMLOps Prefect deployment definitions.

Deploys training_flow as a Prefect deployment with an optional cron schedule
so the pipeline can run automatically without manual intervention.

Usage:
    # Deploy with default nightly schedule (2am UTC daily):
    python pipelines/deploy.py

    # Deploy with a custom cron expression:
    python pipelines/deploy.py --cron "0 3 * * 1"   # every Monday at 3am

    # Deploy without a schedule (manual trigger only):
    python pipelines/deploy.py --no-schedule

    # Deploy for a specific model × dataset only:
    python pipelines/deploy.py --model JPCP --dataset FDataDataset

    # Or use the Exa CLI (preferred operator interface):
    exa pipeline deploy
    exa pipeline deploy --no-schedule

The deployment appears in the Prefect UI at http://localhost:14200 under
Deployments → training_flow. Trigger a run manually from there or via:
    prefect deployment run "training_flow/examlops-nightly"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODELZOO = _REPO_ROOT / "modelzoo"
for _p in (str(_REPO_ROOT), str(_MODELZOO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from prefect import flow  # noqa: E402
from prefect.schedules import Cron  # noqa: E402

from pipelines.pipeline_generator import run_all_flows, training_flow  # noqa: E402


def build_deployment_params(entries: list) -> list[dict]:
    """Extract Prefect deployment parameters from enabled ModelEntry objects.

    Returns one dict per entry with keys: model_name, dataset_names,
    deployment_name, cron, work_pool, concurrency_limit, dummy, backend.
    """
    params = []
    for entry in entries:
        pf = entry.prefect or {}
        params.append(
            {
                "model_name": entry.name,
                "dataset_names": entry.datasets,
                "deployment_name": pf.get(
                    "deployment_name", f"examlops-{entry.name.lower()}-nightly"
                ),
                "cron": pf.get("schedule") or None,
                "work_pool": pf.get("work_pool", "default-agent"),
                "concurrency_limit": pf.get("concurrency_limit", 1),
                "dummy": entry.dummy,
                "backend": entry.backend,
            }
        )
    return params


@flow(name="examlops_scheduled_training")
def scheduled_training_flow(is_dummy: bool = False) -> list[dict]:
    """
    Wrapper flow that trains ALL auto-discovered models × datasets.
    Scheduled via Prefect — triggered automatically on the configured cron.
    """
    return run_all_flows(is_dummy=is_dummy)


def deploy(
    cron: str | None = "0 2 * * *",
    model: str | None = None,
    dataset: str | None = None,
    deployment_name: str = "examlops-nightly",
) -> None:
    prefect_url = os.getenv("PREFECT_API_URL", "http://localhost:14200/api")
    print(f"Deploying to Prefect at {prefect_url}")

    if model and dataset:
        # Single model × dataset deployment
        @flow(name="examlops_targeted_training")
        def _targeted() -> dict:
            return training_flow(model_name=model, dataset_cls_name=dataset, is_dummy=False)

        target_flow = _targeted
        name = f"examlops-{model.lower()}-{dataset.lower()}"
    else:
        target_flow = scheduled_training_flow
        name = deployment_name

    schedule = Cron(cron) if cron else None

    if schedule:
        print(f"Schedule: {cron}")
    else:
        print("No schedule — manual trigger only")

    target_flow.serve(
        name=name,
        schedules=[schedule] if schedule else [],
        tags=["examlops", "training"],
        parameters={"is_dummy": False},
    )


def deploy_from_registry(
    registry_path: str,
    env: str | None = None,
) -> None:
    """Deploy one Prefect flow per enabled model entry in the registry YAML."""
    from pathlib import Path as _Path  # noqa: PLC0415

    from prefect import serve as prefect_serve  # noqa: PLC0415
    from prefect.schedules import Cron  # noqa: PLC0415

    from pipelines.pipeline_generator import training_flow  # noqa: PLC0415
    from pipelines.registry_loader import load_registry  # noqa: PLC0415

    base_path = _Path(registry_path)
    env_path = _Path(f"pipelines/envs/{env}.yaml") if env else None

    all_entries = load_registry(base_path, env_path)
    enabled = [e for e in all_entries if e.enabled]
    params_list = build_deployment_params(enabled)

    deployments = []
    for p in params_list:
        model_name = p["model_name"]
        dataset_names = p["dataset_names"]
        dummy = p["dummy"]
        backend = p["backend"]

        @flow(name=f"examlops_training_{model_name}")
        def _model_flow(
            is_dummy: bool = dummy,
            backend_name: str | None = backend,
            _mn: str = model_name,
            _dns: tuple = tuple(dataset_names),
        ) -> list[dict]:
            results = []
            for ds_name in _dns:
                results.append(
                    training_flow(
                        model_name=_mn,
                        dataset_cls_name=ds_name,
                        is_dummy=is_dummy,
                        backend_name=backend_name,
                    )
                )
            return results

        schedule = Cron(p["cron"]) if p["cron"] else None
        dep = _model_flow.to_deployment(
            name=p["deployment_name"],
            schedules=[schedule] if schedule else [],
            tags=["examlops", "training"],
            work_pool_name=p["work_pool"],
        )
        deployments.append(dep)
        print(f"[deploy] queued '{p['deployment_name']}' for {model_name} (cron={p['cron']!r})")

    if deployments:
        prefect_serve(*deployments)
    else:
        print("[deploy] No enabled models in registry — nothing to deploy.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deploy ExaMLOps training pipeline to Prefect")
    p.add_argument("--cron", default="0 2 * * *", help="Cron expression (default: 2am daily)")
    p.add_argument("--no-schedule", action="store_true", help="Deploy without a schedule")
    p.add_argument("--model", default=None, help="Deploy for a single model only")
    p.add_argument("--dataset", default=None, help="Dataset class name (requires --model)")
    p.add_argument("--name", default="examlops-nightly", help="Deployment name")
    p.add_argument(
        "--registry",
        default=None,
        metavar="PATH",
        help="Path to model_registry.yaml; if provided, deploys one flow per model entry",
    )
    p.add_argument(
        "--env",
        default=None,
        metavar="ENV",
        help="Environment overlay: dev | staging | prod (used only with --registry)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.registry:
        deploy_from_registry(args.registry, env=args.env)
    else:
        deploy(
            cron=None if args.no_schedule else args.cron,
            model=args.model,
            dataset=args.dataset,
            deployment_name=args.name,
        )
