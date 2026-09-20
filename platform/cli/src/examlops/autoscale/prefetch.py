"""Pure prefetch plan — which models to keep warm / pre-pull (ADR 0031 clause 3, planning only).

The weight cache, registry prefetcher and cold-start activator need a runtime and are **not
built**; this is the decision they would consume, and it is read-only.

Rules (deterministic): a model with ``warm_pool > 0`` is ``keep_warm`` always; a model whose policy
allows zero replicas and that saw traffic (``rps > 0``) is ``prefetch`` (it will be called again
and a cold start is expensive); an absent rps is *not* treated as zero traffic. Ranked by rps.
"""

from __future__ import annotations

from typing import Any


def plan_prefetch(
    configs: list[dict[str, Any]],
    rps_by_model: dict[str, float | None],
    *,
    top: int | None = None,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for cfg in configs:
        model = str(cfg["model"])
        rps = rps_by_model.get(model)
        if int(cfg.get("warm_pool") or 0) > 0:
            plan.append(
                {
                    "model": model,
                    "action": "keep_warm",
                    "replicas": int(cfg["warm_pool"]),
                    "rps": rps,
                    "reason": f"warm_pool={cfg['warm_pool']}",
                }
            )
        elif int(cfg.get("min_replicas") or 0) == 0 and rps is not None and rps > 0:
            plan.append(
                {
                    "model": model,
                    "action": "prefetch",
                    "replicas": 1,
                    "rps": rps,
                    "reason": f"can scale to zero but rps={rps:g} > 0",
                }
            )
    plan.sort(key=lambda p: (p["action"] != "keep_warm", -(p["rps"] or 0.0), p["model"]))
    return plan[:top] if top else plan
