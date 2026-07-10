# Example: a custom carbon provider plugin

Copy-paste scaffolding for adding your own carbon methodology to ExaMLOps **without touching its
source** — the plugin path described in [`docs/guides/finops-providers.md`](../../docs/guides/finops-providers.md)
and ADR 0074.

```
exa-carbon-plugin/
├── pyproject.toml                     # declares the exa.providers.carbon entry point
└── src/exa_carbon_plugin/provider.py  # a Provider subclass — edit compute() with your formula
```

## Try it

```bash
pip install ./examples/exa-carbon-plugin
exa finops carbon providers                       # → 'example-fixed-grid' now listed (kind: entrypoint)
exa finops carbon estimate --gpu-hours 12 --provider example-fixed-grid
pip uninstall exa-carbon-plugin-example           # removing the package removes the provider
```

## Make it yours

1. Rename the package (`exa_carbon_plugin` → your name) and the entry-point key in `pyproject.toml`.
2. Edit `FixedGridProvider.compute(inputs) -> {"kwh": ..., "co2e_g": ...}` with your model.
3. Update `metadata()` (methodology text + uncertainty) — the CLI/dashboard surface it.

You can also register providers for other domains (e.g. `[project.entry-points."exa.providers.cost"]`)
the same way.
