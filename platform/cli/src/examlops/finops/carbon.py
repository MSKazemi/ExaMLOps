"""Energy and carbon estimation (#20 Green-AI accounting).

Pure functions so they are unit-testable and reusable by the CLI, the training
pipeline, and (later) a carbon-aware Slurm scheduler. Defaults are conservative,
documented data-centre figures and can all be overridden per call / per env.

References for the defaults:
* GPU TDP 400 W — mid-range data-centre accelerator board power.
* PUE 1.5 — typical (not best-in-class) data-centre power-usage effectiveness.
* Grid intensity 300 gCO2e/kWh — order-of-magnitude EU average; override with the
  real-time value from a grid-intensity API for accuracy.
"""

from __future__ import annotations

# Documented defaults (all overridable).
DEFAULT_GPU_TDP_WATTS = 400.0
DEFAULT_PUE = 1.5
DEFAULT_GRID_INTENSITY_G_PER_KWH = 300.0


def estimate_energy_kwh(
    gpu_hours: float,
    gpu_tdp_watts: float = DEFAULT_GPU_TDP_WATTS,
    pue: float = DEFAULT_PUE,
) -> float:
    """Estimate total facility energy (kWh) for a number of GPU-hours.

    ``kWh = gpu_hours × (tdp_watts / 1000) × pue`` — the PUE factor accounts for
    cooling and other facility overhead beyond the raw GPU draw.
    """
    if gpu_hours < 0 or gpu_tdp_watts < 0 or pue < 0:
        raise ValueError("energy inputs must be non-negative")
    return gpu_hours * (gpu_tdp_watts / 1000.0) * pue


def co2e_grams(
    kwh: float, grid_intensity_g_per_kwh: float = DEFAULT_GRID_INTENSITY_G_PER_KWH
) -> float:
    """Convert energy (kWh) to CO2-equivalent emissions (grams)."""
    if kwh < 0 or grid_intensity_g_per_kwh < 0:
        raise ValueError("carbon inputs must be non-negative")
    return kwh * grid_intensity_g_per_kwh


def estimate_carbon(
    gpu_hours: float,
    gpu_tdp_watts: float = DEFAULT_GPU_TDP_WATTS,
    pue: float = DEFAULT_PUE,
    grid_intensity_g_per_kwh: float = DEFAULT_GRID_INTENSITY_G_PER_KWH,
) -> dict[str, float]:
    """One-shot GPU-hours → {kwh, co2e_g} estimate."""
    kwh = estimate_energy_kwh(gpu_hours, gpu_tdp_watts, pue)
    return {"kwh": kwh, "co2e_g": co2e_grams(kwh, grid_intensity_g_per_kwh)}


def budget_usage_ratio(consumed: float, budget: float | None) -> float | None:
    """Fraction of a budget consumed (``consumed / budget``).

    Returns ``None`` when no budget is set (nothing to enforce) and ``inf`` when a
    positive amount has been consumed against a zero budget.
    """
    if budget is None:
        return None
    if budget == 0:
        return float("inf") if consumed > 0 else 0.0
    return consumed / budget


def estimate_carbon_via_provider(
    gpu_hours: float,
    *,
    provider: str | None = None,
    config: dict | None = None,
    **overrides: float,
) -> dict:
    """GPU-hours → carbon via the **pluggable provider registry** (#20 + FinOps-plugins).

    Resolves the active ``carbon`` provider — an explicit ``provider`` name, else the
    ``[finops.carbon]`` config / ``EXAMLOPS_CARBON_PROVIDER`` env, else the built-in
    ``green-ai-default`` (which reproduces :func:`estimate_carbon` exactly). Configured
    ``coefficients`` are layered *under* per-call ``overrides`` (e.g. ``pue=1.3``) so an explicit
    argument always wins. Returns the provider's outputs plus provenance:
    ``{kwh, co2e_g, provider, methodology, uncertainty}``.

    Degrades gracefully: any resolution error falls back to the default provider so a calculation
    never hard-fails on a bad plugin/config.
    """
    from ..providers import get_provider
    from ..providers.loader import load_domain_config, resolve_provider
    from . import carbon_providers  # noqa: F401 - importing registers the built-ins

    block = dict(config) if config is not None else load_domain_config("carbon")
    coeffs = dict(block.get("coefficients") or {})
    inputs = {**coeffs, **overrides, "gpu_hours": gpu_hours}
    try:
        prov = resolve_provider("carbon", override=provider, config=block)
    except Exception:
        prov = get_provider("carbon", "green-ai-default")
    result = dict(prov.compute(inputs))
    meta = prov.metadata()
    result["provider"] = prov.name
    result["methodology"] = meta.methodology
    result["uncertainty"] = meta.uncertainty
    return result
