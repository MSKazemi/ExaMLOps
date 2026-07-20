"""HPC cost estimation — the **second consumer** of the pluggable provider substrate (ADR 0074).

Demonstrates that ``examlops.providers`` is genuinely general: the same registry that powers carbon
now powers cost, with no substrate change. A **cost provider** turns scheduler usage (GPU-hours, and
optionally CPU-hours) into ``{cost_usd}`` via a swappable **rate card** — flat, tiered, spot-vs-on-demand,
per-cluster — chosen by config, an entry-point plugin, or a declarative YAML formula.

The default (`flat-rate`) reproduces the platform's original arithmetic exactly:
``cost_usd = gpu_hours × gpu_rate + cpu_hours × cpu_rate`` with the same ``GPU_COST_PER_HOUR`` /
``CPU_COST_PER_HOUR`` env defaults — so nothing changes unless a site opts in.
"""

from __future__ import annotations

import os

# Documented default rate card (USD/hour). Env-overridable, matching the original cost command.
DEFAULT_GPU_COST_PER_HOUR = 2.50
DEFAULT_CPU_COST_PER_HOUR = 0.05


def default_gpu_rate() -> float:
    return float(os.getenv("GPU_COST_PER_HOUR", str(DEFAULT_GPU_COST_PER_HOUR)))


def default_cpu_rate() -> float:
    return float(os.getenv("CPU_COST_PER_HOUR", str(DEFAULT_CPU_COST_PER_HOUR)))


def flat_rate_cost(
    gpu_hours: float, cpu_hours: float = 0.0, *, gpu_rate=None, cpu_rate=None
) -> float:
    """The platform's original cost formula: ``gpu_hours × gpu_rate + cpu_hours × cpu_rate``."""
    g = default_gpu_rate() if gpu_rate is None else float(gpu_rate)
    c = default_cpu_rate() if cpu_rate is None else float(cpu_rate)
    if gpu_hours < 0 or cpu_hours < 0:
        raise ValueError("hours must be non-negative")
    return round(gpu_hours * g + cpu_hours * c, 4)


def estimate_cost_via_provider(
    gpu_hours: float,
    cpu_hours: float = 0.0,
    *,
    provider: str | None = None,
    project: str | None = None,
    config: dict | None = None,
    **overrides: float,
) -> dict:
    """GPU/CPU-hours → cost via the pluggable ``cost`` provider registry (ADR 0074).

    Resolution and coefficient layering mirror ``carbon.estimate_carbon_via_provider``: explicit
    ``provider`` → ``EXAMLOPS_COST_PROVIDER`` env → ``[finops.cost]`` config → the built-in
    ``flat-rate`` default. When ``project`` is given, that project's notebook/dashboard-authored
    providers are loaded first so ``--provider <name>`` resolves to them. Returns
    ``{cost_usd, provider, methodology}``; degrades to the default on any resolution error so cost
    recording never hard-fails.
    """
    from ..providers import get_provider
    from ..providers.loader import load_domain_config, resolve_provider
    from . import cost_providers  # noqa: F401 - importing registers the built-ins

    if project:
        try:
            from ..providers import load_project_providers

            load_project_providers(project)
        except Exception:
            pass  # authored providers are additive — never block the built-in path

    block = dict(config) if config is not None else load_domain_config("cost")
    coeffs = dict(block.get("coefficients") or {})
    inputs = {**coeffs, **overrides, "gpu_hours": gpu_hours, "cpu_hours": cpu_hours}
    try:
        prov = resolve_provider("cost", override=provider, config=block)
    except Exception:
        prov = get_provider("cost", "flat-rate")
    result = dict(prov.compute(inputs))
    result["provider"] = prov.name
    result["methodology"] = prov.metadata().methodology
    return result
