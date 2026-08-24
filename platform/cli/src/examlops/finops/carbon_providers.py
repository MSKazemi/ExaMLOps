"""Built-in ``carbon`` calculation providers.

The **first consumer** of the general ``examlops.providers`` substrate. Each provider is a swappable
methodology for turning GPU-hours (and optional coefficients) into ``{kwh, co2e_g}``; the platform
picks one by config, and users/sysadmins can add their own via an entry-point plugin or a declarative
YAML formula (see ``.claude/plans/finops-plugins/``).

Providers registered here:

* ``green-ai-default`` (**default**) — the platform's original formula plus a CPU-core-hours term;
  byte-for-byte the original whenever ``cpu_hours`` is 0, so nothing changes for GPU-only sites.
  The only built-in that can account for a run that used no accelerator.
* ``codecarbon-like`` — component-based energy (GPU + CPU + RAM), after the CodeCarbon/mlco2 approach.
* ``ccf-like`` — Cloud Carbon Footprint's ``usage × energy-coeff × PUE × grid-emissions`` shape.

All three read coefficients from their ``inputs`` mapping (which the consumer builds by layering
configured coefficients under the per-call arguments), so a config override flows through unchanged.
Importing this module registers the providers on the global registry as a side effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..providers import Provider, ProviderMeta, register_provider
from . import carbon

# Extra documented defaults for the component/coefficient models (all overridable).
DEFAULT_CPU_TDP_WATTS = carbon.DEFAULT_CPU_TDP_WATTS  # re-exported; defined with the pure model
DEFAULT_RAM_WATTS_PER_GB = 0.3725  # CodeCarbon's DRAM power model (~3 W / 8 GB)
DEFAULT_RAM_GB = 32.0
DEFAULT_ENERGY_COEFF_KWH_PER_GPU_HOUR = 0.4  # CCF-style pre-PUE energy coefficient


def _f(inputs: Mapping[str, Any], key: str, default: float) -> float:
    """Read a float coefficient from ``inputs`` with a documented fallback."""
    val = inputs.get(key, default)
    return float(val if val is not None else default)


class GreenAIDefaultProvider(Provider):
    """The platform's original methodology — delegates to the untouched pure functions."""

    name = "green-ai-default"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "Energy = (GPU-hours × GPU_TDP + CPU-core-hours × CPU_TDP)/1000 × PUE; "
                "CO₂e = energy × grid intensity. Grid intensity and TDP are estimates; treat "
                "figures as ±30%. With no CPU-hours this is the platform's original GPU-only "
                "formula, unchanged."
            ),
            uncertainty=0.30,
            units={"kwh": "kWh", "co2e_g": "gCO2e"},
            outputs=("kwh", "co2e_g"),
            params=(
                "gpu_hours",
                "gpu_tdp_watts",
                "cpu_hours",
                "cpu_tdp_watts",
                "pue",
                "grid_intensity_g_per_kwh",
            ),
            source="conservative EU data-centre defaults",
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        gpu_hours = _f(inputs, "gpu_hours", 0.0)
        tdp = _f(inputs, "gpu_tdp_watts", carbon.DEFAULT_GPU_TDP_WATTS)
        pue = _f(inputs, "pue", carbon.DEFAULT_PUE)
        grid = _f(inputs, "grid_intensity_g_per_kwh", carbon.DEFAULT_GRID_INTENSITY_G_PER_KWH)
        cpu_hours = _f(inputs, "cpu_hours", 0.0)
        cpu_tdp = _f(inputs, "cpu_tdp_watts", carbon.DEFAULT_CPU_TDP_WATTS)
        # With cpu_hours = 0 this is byte-identical to the legacy estimate_carbon path.
        return carbon.estimate_carbon(
            gpu_hours, tdp, pue, grid, cpu_hours=cpu_hours, cpu_tdp_watts=cpu_tdp
        )


class CodeCarbonLikeProvider(Provider):
    """Component-based energy: GPU + CPU + RAM draw over the run, scaled by PUE (CodeCarbon-style).

    A whole-node model charged over **GPU-hours**: the CPU and RAM terms are the draw of the node
    hosting the accelerators, not an independent CPU workload. It therefore declares no
    ``cpu_hours`` parameter, and the seam refuses a call that supplies them rather than dropping
    them — extending this formula to CPU-only runs would be inventing a methodology, not applying
    CodeCarbon's.
    """

    name = "codecarbon-like"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "Energy = GPU-hours × (GPU_TDP + CPU_TDP + RAM_GB × RAM_W/GB)/1000 × PUE; "
                "CO₂e = energy × grid intensity. After the CodeCarbon/mlco2 component model."
            ),
            uncertainty=0.25,
            units={"kwh": "kWh", "co2e_g": "gCO2e"},
            outputs=("kwh", "co2e_g"),
            params=(
                "gpu_hours",
                "gpu_tdp_watts",
                "cpu_tdp_watts",
                "ram_gb",
                "ram_watts_per_gb",
                "pue",
                "grid_intensity_g_per_kwh",
            ),
            source="https://github.com/mlco2/codecarbon",
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        gpu_hours = _f(inputs, "gpu_hours", 0.0)
        gpu_tdp = _f(inputs, "gpu_tdp_watts", carbon.DEFAULT_GPU_TDP_WATTS)
        cpu_tdp = _f(inputs, "cpu_tdp_watts", DEFAULT_CPU_TDP_WATTS)
        ram_gb = _f(inputs, "ram_gb", DEFAULT_RAM_GB)
        ram_w_per_gb = _f(inputs, "ram_watts_per_gb", DEFAULT_RAM_WATTS_PER_GB)
        pue = _f(inputs, "pue", carbon.DEFAULT_PUE)
        grid = _f(inputs, "grid_intensity_g_per_kwh", carbon.DEFAULT_GRID_INTENSITY_G_PER_KWH)
        if gpu_hours < 0:
            raise ValueError("gpu_hours must be non-negative")
        watts = gpu_tdp + cpu_tdp + ram_gb * ram_w_per_gb
        kwh = gpu_hours * (watts / 1000.0) * pue
        return {"kwh": kwh, "co2e_g": carbon.co2e_grams(kwh, grid)}


class CCFLikeProvider(Provider):
    """Cloud Carbon Footprint shape: usage × energy-coefficient × PUE × grid-emissions.

    The coefficient is per GPU-hour, so like ``codecarbon-like`` this declares no ``cpu_hours``
    term; a CPU coefficient would be a number nobody here has measured.
    """

    name = "ccf-like"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "Energy = GPU-hours × energy_coeff_kwh_per_gpu_hour × PUE; "
                "CO₂e = energy × grid intensity. After Cloud Carbon Footprint (Thoughtworks)."
            ),
            uncertainty=0.30,
            units={"kwh": "kWh", "co2e_g": "gCO2e"},
            outputs=("kwh", "co2e_g"),
            params=(
                "gpu_hours",
                "energy_coeff_kwh_per_gpu_hour",
                "pue",
                "grid_intensity_g_per_kwh",
            ),
            source="https://www.cloudcarbonfootprint.org/docs/methodology/",
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        gpu_hours = _f(inputs, "gpu_hours", 0.0)
        coeff = _f(inputs, "energy_coeff_kwh_per_gpu_hour", DEFAULT_ENERGY_COEFF_KWH_PER_GPU_HOUR)
        pue = _f(inputs, "pue", carbon.DEFAULT_PUE)
        grid = _f(inputs, "grid_intensity_g_per_kwh", carbon.DEFAULT_GRID_INTENSITY_G_PER_KWH)
        if gpu_hours < 0 or coeff < 0:
            raise ValueError("carbon inputs must be non-negative")
        kwh = gpu_hours * coeff * pue
        return {"kwh": kwh, "co2e_g": carbon.co2e_grams(kwh, grid)}


class GridLiveProvider(Provider):
    """green-ai energy formula with a **live** grid carbon-intensity signal (BL-004).

    Identical to ``green-ai-default`` except the grid intensity is fetched from a configured
    endpoint (``EXAMLOPS_GRID_INTENSITY_URL``) instead of a static default — so carbon figures
    track when the grid is clean vs dirty. Degrades to the static default offline, and an explicit
    ``grid_intensity_g_per_kwh`` input always wins over the live signal.
    """

    name = "grid-live"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "Energy = (GPU-hours × GPU_TDP + CPU-core-hours × CPU_TDP)/1000 × PUE; "
                "CO₂e = energy × LIVE grid intensity fetched from EXAMLOPS_GRID_INTENSITY_URL "
                "(cached, ~5 min). Degrades to the static default when no endpoint is configured "
                "or a fetch fails."
            ),
            uncertainty=0.20,
            units={"kwh": "kWh", "co2e_g": "gCO2e"},
            outputs=("kwh", "co2e_g"),
            params=(
                "gpu_hours",
                "gpu_tdp_watts",
                "cpu_hours",
                "cpu_tdp_watts",
                "pue",
                "grid_intensity_g_per_kwh",
            ),
            source="live grid-intensity endpoint (operator-configured)",
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        gpu_hours = _f(inputs, "gpu_hours", 0.0)
        tdp = _f(inputs, "gpu_tdp_watts", carbon.DEFAULT_GPU_TDP_WATTS)
        pue = _f(inputs, "pue", carbon.DEFAULT_PUE)
        # An explicit grid override wins; otherwise use the live signal (which itself degrades
        # to the platform's static default when unavailable).
        if inputs.get("grid_intensity_g_per_kwh") is not None:
            grid = _f(inputs, "grid_intensity_g_per_kwh", carbon.DEFAULT_GRID_INTENSITY_G_PER_KWH)
        else:
            from .grid_intensity import current_grid_intensity

            grid = current_grid_intensity(carbon.DEFAULT_GRID_INTENSITY_G_PER_KWH)
        # Stays what its docstring says it is: green-ai-default's energy model, live grid signal.
        return carbon.estimate_carbon(
            gpu_hours,
            tdp,
            pue,
            grid,
            cpu_hours=_f(inputs, "cpu_hours", 0.0),
            cpu_tdp_watts=_f(inputs, "cpu_tdp_watts", carbon.DEFAULT_CPU_TDP_WATTS),
        )


def register_builtins() -> None:
    """Register the built-in carbon providers on the global registry (idempotent)."""
    register_provider("carbon", "green-ai-default", GreenAIDefaultProvider, default=True)
    register_provider("carbon", "codecarbon-like", CodeCarbonLikeProvider)
    register_provider("carbon", "ccf-like", CCFLikeProvider)
    register_provider("carbon", "grid-live", GridLiveProvider)


register_builtins()
