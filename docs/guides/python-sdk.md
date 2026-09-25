# Python SDK — `import examlops`

The `examlops` package is a typed, versioned Python API over the platform. The `exa` CLI and the
MCP tools call the same functions, so a script, a notebook, an agent and an operator at the
terminal all go through one code path.

```python
import examlops

print(examlops.api_version())          # "0.2" — version of the SDK contract
st = examlops.status()                 # PlatformStatus (typed, not a dict)
for m in examlops.models.list():       # every registered model, all pages
    print(m.name, m.production_version, m.latest_version)
```

## What is public

The public surface is everything in `examlops.__all__`, plus the `__all__` of each namespace
below. Anything else, including every `examlops._*` module and every internal module the SDK wraps
(`cli._client`, `platform_db`, `hpc_registry`, …), is private and can change without notice.
To print the current surface with signatures, run:

```bash
exa docs --sdk              # Markdown reference, generated from the code
exa --json docs --sdk       # the same, machine-readable
```

The MCP agent card (`exa mcp agent-card`) carries the same contract under its `sdk` key, so
agents and humans read the same surface.

| Namespace | Functions | Returns |
|---|---|---|
| `examlops` | `status()`, `place(...)`, `list_providers(domain)`, `resolve_provider(domain)`, `api_version()` | `PlatformStatus`, `PlacementResult`, … |
| `examlops.models` | `list()`, `get(name)`, `diff(name, v1, v2)`, `lineage(name, version=None)`, `cost(name)` | `ModelSummary`, `ModelDetail`, `ModelDiff`, `Lineage`, `CostRecord` |
| `examlops.models` (mutating) | `retrain(...)`, `approve(...)`, `promote(...)` | `RetrainResult`, `ApproveResult`, `PromoteResult` |
| `examlops.drift` | `status(model=None)` | `DriftStatus` |
| `examlops.audit` | `query(since_days=30, model=, action=, source=, actor=, tenant=, limit=100)` | `AuditEvent` |
| `examlops.hpc` | `place(...)`, `clusters()`, `capacity()` | `Cluster`, `ClusterCapacity` |
| `examlops.sdk` | pipeline DSL (`pipeline`, `step`, `train`, …), `Result`/`ok`/`err`, onboarding | |

Every result is a frozen dataclass with a `to_dict()` method. Where a CLI command already had a
`--json` document, `to_dict()` returns that same document.

## Errors

Every failure is an `examlops.SDKError`. Catch a subclass from `examlops.sdk.errors` to handle a
specific case:

| Error | Meaning |
|---|---|
| `NotFoundError` | the model, version or run does not exist (HTTP 404) |
| `UnavailableError` | MLflow, the control plane or the platform datastore could not be reached |
| `IncompleteReadError` | a paged read could not finish; the SDK returns no partial list |
| `InvalidArgumentError` | the SDK rejected an argument before contacting anything (also a `ValueError`) |
| `ConfirmationRequiredError` | a mutating call without `confirm=True` |
| `PolicyDeniedError` | a policy-as-code rule (ADR 0079) denied the call |
| `ApprovalRequiredError` | a `require_approval` rule applies and `approved=True` was not passed |
| `GateRefusedError` | a promotion gate refused (eval, parity, SLO, compliance, fairness, synthetic-only) |

`err.status` is the upstream HTTP status, when there was one.

## Mutating calls

The SDK applies the same rules as the CLI (dry run, confirmation, policy, audit):

- `dry_run=True` shows what would happen and changes nothing.
- Any other call needs `confirm=True`. A program has no keyboard, so consent is an argument you
  pass, never a default.
- The ADR 0079 policy engine is consulted. A `deny` rule raises `PolicyDeniedError`. A
  `require_approval` rule raises `ApprovalRequiredError` unless you pass `approved=True`. Pass it
  only once a human has approved.
- A completed mutation writes an audit event (`source="sdk"`, actor `EXAMLOPS_ACTOR`). If the
  audit write fails, the operation still stands, the loss is counted, and the result has
  `audited=False`.

```python
from examlops.sdk.errors import ApprovalRequiredError

preview = examlops.models.retrain("JPCP", "PM100Dataset", dry_run=True)
run = examlops.models.retrain("JPCP", "PM100Dataset", confirm=True, reason="drift on JPCP")
print(run.dispatched, run.flow_run_id or run.command_id)

examlops.models.approve("JPCP", confirm=True)          # policy action `model_approve`

try:
    examlops.models.promote(
        "jpcp", metric="rmse", operator="lt", threshold=5.0, confirm=True
    )
except ApprovalRequiredError:
    ...  # get a human's approval, then call again with approved=True
```

`promote` runs `exa pipeline promote` in a child process with a timeout (default 300 s), so every
promotion gate and audit event is exactly the CLI's. The SDK does not keep a second copy of those
gates. The SDK checks model names, aliases and metric names before it starts the child, so a
value that starts with `-` can never be read as an option. `force=True` overrides the built-in
metric gates, and the override is audited, as with `--force`. It never overrides a policy deny.
If a `require_approval` rule exists for `manual_promote`, the SDK asks for `approved=True` before
it starts the child. The child runs with `--yes`, so without this check the flag would answer the
approval prompt.

`exa approvals approve` itself calls `examlops.models.approve`, so the CLI checks the new
`model_approve` policy action too. With no matching rule nothing changes: the call is allowed and
no policy decision is audited. When a `require_approval` rule matches, the confirmation prompt
says `[policy requires approval]` and defaults to no, as `exa retrain`'s does: pressing Enter
approves nothing, and only an explicit `y` (or `--yes`) gives the approval.

## Reading the audit log

```python
events = examlops.audit.query(since_days=7, action="model_approved", tenant="acme", limit=500)
```

`query` filters in SQL before the `LIMIT` is applied, including the tenant filter, so another
tenant's newer events cannot push yours out of the window. Rows come newest first, in the hash
chain's own order. `limit` is capped at 10 000 rows (`examlops.audit.MAX_LIMIT`), and
`exa audit -n` is capped at the same value.

## Versioning and deprecation policy

- `examlops.api_version()` follows SemVer for the public surface. It is separate from the package
  version (`examlops.__version__`).
- Adding a name is a minor bump.
- A public name is removed only after it has been deprecated for at least one minor release. It
  is wrapped with `examlops.sdk.deprecation.deprecated(since=..., removed_in=...,
  replacement=...)`, which emits a `DeprecationWarning` naming the replacement on every call.
  While the API is `0.x`, the earliest removal is the next minor. From `1.0`, it is the next
  major.
- `tests/unit/test_sdk_deprecation_policy.py` enforces this. A schedule shorter than the window
  is refused when the module loads, and the build fails if a deprecated name is still exported
  after its `removed_in` version.
- Keep the surface small. Add a name when a caller needs it, not in advance.

## Typing

The package ships a `py.typed` marker (PEP 561), so mypy and pyright check your code against the
SDK's annotations. `tests/unit/test_sdk_reference.py` checks that every public function annotates
all of its parameters and its return value, and that every public name has a docstring.

## Reserved names

`examlops.models`, `examlops.drift`, `examlops.audit` and `examlops.hpc` are aliases of the
`examlops.sdk.*` namespaces. `import examlops.models` works too. The package must never gain a
real module with one of these names. `tests/unit/test_sdk_namespaces.py` fails the build if one
appears.
