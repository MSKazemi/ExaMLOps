"""Energy and carbon estimation (#20 Green-AI accounting).

Pure functions so they are unit-testable and reusable by the CLI, the training
pipeline, and (later) a carbon-aware Slurm scheduler. Defaults are conservative,
documented data-centre figures and can all be overridden per call / per env.

References for the defaults:
* GPU TDP 400 W — mid-range data-centre accelerator board power.
* CPU TDP 120 W — a typical server CPU package.
* PUE 1.5 — typical (not best-in-class) data-centre power-usage effectiveness.
* Grid intensity 300 gCO2e/kWh — order-of-magnitude EU average; override with the
  real-time value from a grid-intensity API for accuracy.

**CPU-hours are a first-class input, not an optional extra.** Accounting only for GPU-hours makes
every CPU-only run emit exactly zero, which is the best possible number and never the true one —
and CPU-only is not a corner case here: the platform runs on Flux and Slurm sites whose nodes have
no accelerators at all, and ``exa models cost`` already reads ``(gpu_hours, cpu_hours)`` per job
from the scheduler. A run that used no GPU still burned energy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module import-light
    from .carbon_signal import CarbonSignal

# Documented defaults (all overridable).
DEFAULT_GPU_TDP_WATTS = 400.0
DEFAULT_CPU_TDP_WATTS = 120.0
DEFAULT_PUE = 1.5
DEFAULT_GRID_INTENSITY_G_PER_KWH = 300.0


#: ADR 0112 R-ee. Every carbon figure this platform records is **operational** — energy × grid
#: intensity. Embodied carbon (manufacturing the GPUs, servers and network) is not measured, and
#: as grids decarbonise it comes to dominate (Acun et al., *Carbon Explorer*, ASPLOS '23). So an
#: operational sum is never a total: a total would understate emissions, in the direction that
#: flatters the platform. ``carbon_scope`` is the one definition every surface reports through.
SCOPE_OPERATIONAL = "operational"
EMBODIED_UNAVAILABLE = (
    "unavailable — embodied (manufacturing) carbon is not measured, so this is not a total"
)


def carbon_scope(operational_g: float | None) -> dict[str, object]:
    """Report a carbon sum with its scope: operational figure, embodied unavailable, no total.

    ``operational_g`` is ``None`` when nothing was measured — reported as ``None``, never 0.0,
    because "0 kg CO2e" for an unmeasured platform reads as an achievement. ``total_kg_co2e`` is
    always ``None`` until embodied carbon is measured: an operational-only figure MUST NOT be
    reported as a total (R-ee), and unavailable embodied carbon is reported as unavailable, never
    as zero (P5).
    """
    return {
        "scope": SCOPE_OPERATIONAL,
        "operational_kg_co2e": None if operational_g is None else round(operational_g / 1000.0, 3),
        "embodied_kg_co2e": None,
        "embodied": EMBODIED_UNAVAILABLE,
        "total_kg_co2e": None,
    }


class CarbonInputUnaccounted(ValueError):
    """The chosen provider has no term for an input it was given.

    Raised instead of dropping the input and returning a smaller number. A carbon figure that
    quietly omits part of the work is worse than no figure: it is publishable, plausible and low.
    """


def estimate_energy_kwh(
    gpu_hours: float,
    gpu_tdp_watts: float = DEFAULT_GPU_TDP_WATTS,
    pue: float = DEFAULT_PUE,
    *,
    cpu_hours: float = 0.0,
    cpu_tdp_watts: float = DEFAULT_CPU_TDP_WATTS,
) -> float:
    """Estimate total facility energy (kWh) for GPU-hours and CPU-core-hours.

    ``kWh = (gpu_hours × gpu_tdp + cpu_hours × cpu_tdp) / 1000 × pue`` — the PUE factor accounts
    for cooling and other facility overhead beyond the raw device draw.

    ``cpu_hours`` defaults to 0, so a GPU-only call is arithmetically identical to what this
    function has always returned.
    """
    if gpu_hours < 0 or gpu_tdp_watts < 0 or pue < 0:
        raise ValueError("energy inputs must be non-negative")
    if cpu_hours < 0 or cpu_tdp_watts < 0:
        raise ValueError("energy inputs must be non-negative")
    watt_hours = gpu_hours * gpu_tdp_watts + cpu_hours * cpu_tdp_watts
    return watt_hours / 1000.0 * pue


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
    *,
    cpu_hours: float = 0.0,
    cpu_tdp_watts: float = DEFAULT_CPU_TDP_WATTS,
) -> dict[str, float]:
    """One-shot device-hours → {kwh, co2e_g} estimate."""
    kwh = estimate_energy_kwh(
        gpu_hours, gpu_tdp_watts, pue, cpu_hours=cpu_hours, cpu_tdp_watts=cpu_tdp_watts
    )
    return {"kwh": kwh, "co2e_g": co2e_grams(kwh, grid_intensity_g_per_kwh)}


def estimate_emissions(
    gpu_hours: float,
    signal: CarbonSignal,
    gpu_tdp_watts: float = DEFAULT_GPU_TDP_WATTS,
    pue: float = DEFAULT_PUE,
    *,
    cpu_hours: float = 0.0,
    cpu_tdp_watts: float = DEFAULT_CPU_TDP_WATTS,
) -> dict:
    """Device-hours → emissions, from a **typed accounting signal** (ADR 0112 decisions 2/4/6).

    This is :func:`estimate_carbon` with the one thing a bare float cannot carry: what the
    intensity figure measures. A marginal/decision signal is **rejected**, not silently used —
    it overstates allocated emissions and would make EED reporting wrong in the other
    direction. The result states its own ``method`` and ``signal_type`` so no carbon figure
    this platform emits can be read without knowing which kind it is (decision 6).
    """
    from .carbon_signal import require_accounting

    require_accounting(signal, path="estimate_emissions()")
    out = estimate_carbon(
        gpu_hours,
        gpu_tdp_watts,
        pue,
        signal.grams_per_kwh,
        cpu_hours=cpu_hours,
        cpu_tdp_watts=cpu_tdp_watts,
    )
    return {**out, "signal": signal.as_dict()}


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
    cpu_hours: float = 0.0,
    provider: str | None = None,
    project: str | None = None,
    config: dict | None = None,
    **overrides: float,
) -> dict:
    """Device-hours → carbon via the **pluggable provider registry** (#20 + FinOps-plugins).

    ``cpu_hours`` is CPU-core-hours, the second half of what ``exa models cost`` already reads
    from the scheduler. If the resolved provider declares no ``cpu_hours`` term this raises
    :class:`CarbonInputUnaccounted` instead of returning a number that leaves them out.

    Resolves the active ``carbon`` provider — an explicit ``provider`` name, else the project's
    notebook/dashboard-authored active provider (when ``project`` is given), else the
    ``[finops.carbon]`` config / ``EXAMLOPS_CARBON_PROVIDER`` env, else the built-in
    ``green-ai-default`` (which reproduces :func:`estimate_carbon` exactly). Configured
    ``coefficients`` are layered *under* per-call ``overrides`` (e.g. ``pue=1.3``) so an explicit
    argument always wins. Returns the provider's outputs plus provenance:
    ``{kwh, co2e_g, provider, methodology, uncertainty}``.

    Degrades gracefully: any resolution error falls back to the default provider so a calculation
    never hard-fails on a bad plugin/config.
    """
    from ..providers import get_provider
    from ..providers.loader import degraded_to_default, load_domain_config, resolve_provider
    from . import carbon_providers  # noqa: F401 - importing registers the built-ins

    if project:
        try:
            from ..providers import get_active_provider, load_project_providers

            load_project_providers(project)
            if provider is None:
                provider = get_active_provider(project, "carbon")
        except Exception as exc:  # noqa: BLE001 - a broken provider must not stop the calculation
            degraded_to_default("carbon", exc)
            pass  # authored providers are additive — never block the built-in path

    block = dict(config) if config is not None else load_domain_config("carbon")
    coeffs = dict(block.get("coefficients") or {})
    inputs = {**coeffs, **overrides, "gpu_hours": gpu_hours, "cpu_hours": cpu_hours}
    try:
        prov = resolve_provider("carbon", override=provider, config=block)
    except Exception as exc:  # noqa: BLE001 - a broken provider must not stop the calculation
        degraded_to_default("carbon", exc)
        prov = get_provider("carbon", "green-ai-default")
    meta = prov.metadata()
    # Refuse rather than drop. A provider that has no `cpu_hours` term would silently return the
    # GPU-only number, which on a CPU-only run is zero — a low, plausible, publishable figure for
    # work that really happened. Degrading gracefully is for an unavailable dependency, not for an
    # input the caller measured and handed over.
    if cpu_hours and "cpu_hours" not in meta.params:
        raise CarbonInputUnaccounted(
            f"carbon provider {prov.name!r} has no term for cpu_hours "
            f"(it accounts for: {', '.join(meta.params)}). Refusing to report a figure that "
            f"silently omits {cpu_hours} CPU-core-hours — choose a provider that accounts for "
            "them (e.g. green-ai-default) or pass --cpu-hours 0 if they are genuinely not yours."
        )
    result = dict(prov.compute(inputs))
    result["provider"] = prov.name
    result["methodology"] = meta.methodology
    result["uncertainty"] = meta.uncertainty
    return result
