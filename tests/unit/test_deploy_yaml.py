# tests/unit/test_deploy_yaml.py
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipelines.registry_loader import load_registry  # noqa: E402

DEPLOY_REGISTRY = textwrap.dedent("""
    version: "1"
    defaults:
      backend: zenodo
      dummy: false
      enabled: true
      serve_aliases: [Production]
    models:
      - name: JPCP
        model_class: JPCP
        datasets: [PM100Dataset]
        backend: minio
        lifecycle: []
        serve_aliases: [Production]
        prefect:
          schedule: "0 3 * * *"
          deployment_name: examlops-jpcp-nightly
          work_pool: my-pool
          concurrency_limit: 2
      - name: MACK
        model_class: MACK
        datasets: [FDataDataset]
        lifecycle: []
        serve_aliases: [Production]
        prefect:
          schedule: null
          deployment_name: examlops-mack-nightly
          work_pool: my-pool
          concurrency_limit: 1
""")


def test_build_deployment_params_returns_one_per_enabled_entry(tmp_path):
    from pipelines.deploy import build_deployment_params
    f = tmp_path / "reg.yaml"
    f.write_text(DEPLOY_REGISTRY)
    entries = [e for e in load_registry(f) if e.enabled]
    params = build_deployment_params(entries)
    assert len(params) == 2


def test_build_deployment_params_reads_prefect_schedule(tmp_path):
    from pipelines.deploy import build_deployment_params
    f = tmp_path / "reg.yaml"
    f.write_text(DEPLOY_REGISTRY)
    entries = [e for e in load_registry(f) if e.enabled]
    params = build_deployment_params(entries)
    jpcp = next(p for p in params if p["model_name"] == "JPCP")
    assert jpcp["cron"] == "0 3 * * *"
    assert jpcp["deployment_name"] == "examlops-jpcp-nightly"
    assert jpcp["work_pool"] == "my-pool"
    assert jpcp["backend"] == "minio"


def test_build_deployment_params_null_schedule(tmp_path):
    from pipelines.deploy import build_deployment_params
    f = tmp_path / "reg.yaml"
    f.write_text(DEPLOY_REGISTRY)
    entries = [e for e in load_registry(f) if e.enabled]
    params = build_deployment_params(entries)
    mack = next(p for p in params if p["model_name"] == "MACK")
    assert mack["cron"] is None
