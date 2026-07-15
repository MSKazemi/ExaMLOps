# Provider Security & Trust Tiers (ADR 0081)

ExaMLOps supports three ways to plug in a calculation provider. Each tier has different security
properties and is appropriate for different authors.

## The three tiers

### Tier 1 — Built-in Python provider (trusted)

Shipped inside the `examlops` package. Reviewed and audited as part of the core codebase. Runs
with full Python privileges. Reserved for platform maintainers.

```python
class GreenAIDefaultProvider(Provider):
    name = "green-ai-default"
    def compute(self, inputs): ...
```

### Tier 2 — Entry-point plugin (trusted)

A `pip`-installable package registered under the `exa.providers.<domain>` entry-point group.
Treated the same as Tier 1: it runs as normal Python, so it can do anything the installer's
permission grants. Appropriate for site operators who publish their own rate cards or scoring
libraries.

```toml
# pyproject.toml of a plugin package
[project.entry-points."exa.providers.carbon"]
my-formula = "my_package.providers:MyCarbonProvider"
```

### Tier 3 — Declarative YAML formula (sandboxed)

A formula written in `~/.config/examlops/providers.yaml`. Evaluated by `simpleeval` — an
AST-walking interpreter that forbids imports, attribute access, comprehensions, and lambdas. Only
pure arithmetic plus a curated `SAFE_FUNCTIONS` allow-list is available.

```yaml
# ~/.config/examlops/providers.yaml
[carbon]
provider = "expression"
[carbon.formulas]
kwh     = "gpu_hours * 0.35 * pue"
co2e_g  = "kwh * 500"
```

**Cannot** read files, access environment variables, exfiltrate prompts, or call external APIs.
Appropriate for sysadmins who want to adjust coefficients or swap a formula without writing Python.

## The `SAFE_FUNCTIONS` allow-list

The sandboxed evaluator exposes only these math helpers (from `examlops/providers/expression.py`):

| Function | Provided by |
|---|---|
| `min`, `max`, `abs` | built-ins |
| `round`, `pow`, `sum` | built-ins |
| `log`, `log10`, `log2` | `math` |
| `exp`, `sqrt`, `floor`, `ceil` | `math` |

**Adding a function to `SAFE_FUNCTIONS` is a security-boundary change.** It widens what a
Tier 3 formula author can call. Any such change must:

1. Include a security-review note in the commit message.
2. Update `_REVIEWED_SAFE_FUNCTIONS` in `tests/unit/test_provider_security.py` (the guard test
   will fail until this is done, blocking CI).

## Security guard tests

`tests/unit/test_provider_security.py` enforces three structural invariants:

| Test | What it checks |
|---|---|
| `test_no_raw_eval_exec_in_sandboxed_modules` | `expression.py`, `yaml_provider.py`, `loader.py` contain no bare `eval`/`exec`/`compile` calls |
| `test_safe_functions_match_reviewed_set` | `SAFE_FUNCTIONS` equals the last-reviewed allow-list exactly |
| `test_sandbox_rejects_dangerous_expressions` | `__import__`, `__class__.__bases__`, `open`, comprehensions, lambdas all raise `ProviderError` |

These tests run in CI with every push. A failed `test_safe_functions_match_reviewed_set` is the
signal to update the allow-list with a security review, not to update the test silently.

## Trust-tier decision tree

```
Who writes the provider?
├── Platform team, ships in examlops package → Tier 1 (built-in)
├── Site operator, ships as pip package      → Tier 2 (entry-point plugin)
└── Sysadmin, edits providers.yaml locally  → Tier 3 (sandboxed YAML formula)
                                               └── arithmetic only; no imports
```

## Related

- ADR 0074 — Generic `examlops.providers` substrate
- ADR 0077 — Provider domains (all non-finops domains use `providers.yaml`)
- ADR 0081 — Trust-tier security contract (the formal decision)
- `examlops/providers/expression.py` — sandboxed evaluator implementation
