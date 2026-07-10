"""Example ExaMLOps carbon provider plugin.

Copy this package, rename it, and edit :meth:`FixedGridProvider.compute` to implement your own
carbon methodology. After ``pip install .`` the provider is discovered automatically via the
``exa.providers.carbon`` entry point declared in ``pyproject.toml`` and appears in
``exa finops carbon providers`` (kind: ``entrypoint``).

A provider only has to implement ``compute(inputs) -> {"kwh": ..., "co2e_g": ...}``. Implementing
``metadata()`` is optional but recommended — it drives the methodology/uncertainty the CLI and
dashboard display.
"""

from __future__ import annotations

from examlops.providers import Provider, ProviderMeta


class FixedGridProvider(Provider):
    """A trivial custom methodology: the platform's energy model on a fixed, low-carbon grid.

    Demonstrates reading call inputs with documented fallbacks and pinning a coefficient (here the
    grid intensity) inside the provider itself.
    """

    name = "example-fixed-grid"
    version = "0.1.0"

    #: A fixed grid intensity this provider always assumes (e.g. a nuclear/hydro-heavy region).
    GRID_INTENSITY_G_PER_KWH = 50.0

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "Energy = GPU-hours × (TDP/1000) × PUE; CO₂e = energy × 50 gCO2e/kWh "
                "(a fixed low-carbon grid). Example plugin — replace with your own model."
            ),
            uncertainty=0.20,
            units={"kwh": "kWh", "co2e_g": "gCO2e"},
            outputs=("kwh", "co2e_g"),
            params=("gpu_hours", "gpu_tdp_watts", "pue"),
            source="example plugin",
        )

    def compute(self, inputs):
        gpu_hours = float(inputs.get("gpu_hours", 0.0))
        tdp = float(inputs.get("gpu_tdp_watts", 400.0))
        pue = float(inputs.get("pue", 1.5))
        if gpu_hours < 0:
            raise ValueError("gpu_hours must be non-negative")
        kwh = gpu_hours * (tdp / 1000.0) * pue
        return {"kwh": kwh, "co2e_g": kwh * self.GRID_INTENSITY_G_PER_KWH}
