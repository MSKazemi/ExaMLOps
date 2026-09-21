# Pipeline as code

Author a training pipeline in typed Python, compile it to a versioned, hashed IR, review it, and run
it through the same generator and Prefect flow that YAML-authored models use. YAML authoring
(`usecases/<pack>/models/<name>.yaml`) stays first-class and unchanged; this is an additive second
way in.

## Write a pipeline

```python
from examlops.sdk import dataset, evaluate, pipeline, promote, train

@pipeline(name="JPCP", serving={"model_id": "jpcp"})
def jpcp():
    pm100 = dataset("PM100Dataset", backend="zenodo", cache_dir=".data_cache/pm100")
    model = train(pm100, config_class="jpcp_config.JPCPConfiguration",
                  task_type="regression", framework="sklearn")
    promote(evaluate(model), lifecycle=[
        {"name": "Production", "metric": "rmse", "threshold": 50.0, "direction": "lower_is_better"}])
```

A complete twin of the reference `jpcp.yaml` is `examples/pipeline-as-code/jpcp_flow.py`.

* The helpers **record steps, they do not run them**. Each returns a handle to its typed output;
  passing a handle to another step records the edge.
* `@pipeline(...)` keywords other than `name`/`kind`/`cluster` are the matching per-model YAML
  sections (`serving`, `prefect`, `inference`, `project`, `enabled`, `dataplane_bus_uuid`, `engine`,
  `fairness`, `autoscale`), carried through unchanged.
* `resources=Resources(gpus=..., cpus=..., nodes=...)` on `train`, and `cluster="name|auto"` on the
  pipeline, are run-time placement asks (reusing `--cluster` / `--gpus`).
* Dataset order is the order you pass them to `train`; it is the run order and is part of the hash.

## Compile, explain, run

```bash
exa pipeline compile flows/jpcp.py --out jpcp.ir.json          # print / write the IR + hash
exa pipeline compile flows/jpcp.py --out jpcp.ir.json --yaml jpcp.yaml   # also lower to YAML
exa pipeline explain jpcp.ir.json                              # read-only topological plan
exa pipeline run --ir jpcp.ir.json --dummy                     # train through the real generator
```

`compile` exits 1 on a validation error, on an unlowerable pipeline when `--yaml` is given, or when
policy denies it. `--out` and `--yaml` write files; the IR is deterministic, so the same pipeline
always produces the same `content_hash`, and editing the JSON afterwards is caught on load.

## The IR

`schema_version: 1`, `kind: training`, `name`, `nodes` (id, kind, params, typed `inputs`/`outputs`,
`resources`), `edges` (`from`/`output` to `to`/`input`), `registry` (the YAML sections above) and
`target`. Step kinds: `dataset`, `train`, `evaluate`, `promote` (lower today) and `hpo`,
`custom_python` (valid, explainable, **not lowerable yet**).

Validation refuses: cycles, dangling edges, duplicate step ids, type mismatches between ports,
unconnected required inputs, unknown step kinds, unknown or missing params, non-JSON values, an
unknown `schema_version` or pipeline kind, and a `content_hash` that no longer matches.

## How it runs

`run --ir` lowers the IR to the per-model YAML mapping, writes it to a temporary file and starts the
normal generator with `--model-yaml`, which registers it exactly as a pack YAML is registered. The
`config_class` shim must exist in the active use-case pack, as for any model. The equivalence is
tested two ways: the lowered YAML of the example loads to the *identical* `ModelYAMLConfig` as the
reference YAML (always on), and a seeded dummy-data training through both paths reports identical
metrics and registration (opt-in: `EXAMLOPS_LIVE_PIPELINE_EQUIV=1 pytest -m live
tests/integration/test_pipeline_dsl_run_equivalence.py`, about 40 s).

Refused rather than skipped: an unlowerable step kind, a dataset the train step does not consume,
a second train step, a missing evaluate step, an evaluate split other than `validation`. `run --ir`
supports the inline (mock) scheduler only, because a Slurm/Flux node re-loads models from the pack
and would not know an IR-only model; for real HPC, lower with `--yaml` into the pack's `models/`
directory, review and commit it, then run it by name. The YAML has no field for `resources` or
`target.cluster`; `compile --yaml` lists what it dropped, and `run --ir` uses them as placement hints.

## Trust: what is and is not sandboxed

A pipeline file is **operator-written, trusted-tier Python** (ADR 0081's tier 2): `exa pipeline
compile` executes it with your privileges, and nothing prevents a body from reading files or
making network calls. "Declarative" here is a convention the helpers follow, not something enforced.

`--untrusted` adds the provider AST allow-list before loading (no imports, no `open`/`eval`/`exec`/
`getattr`, no dunder attribute access, no `global`/`nonlocal`), with the DSL names pre-injected and
restricted builtins. That is a **static gate for authenticated authors, not a hardened jail**, the
same limit as authored providers. The policy hook runs after the file has been traced, on the
compiled IR, so it governs the output, not the act of executing the file. In the dashboard CLI
console `pipeline compile` is admin-tier for this reason.

## Policy

`pipeline_compile` and `pipeline_run_ir` are consulted through the same gate as `manual_promote`
(see [policy-as-code](policy-as-code.md)). Facts in the context: `pipeline`, `kind`, `content_hash`,
`steps`, `step_kinds`, `datasets`, `gpus`, `cluster`, `actor` (and `untrusted`, `source`). No policy
or no matching rule leaves behaviour and the audit trail unchanged.

```yaml
policies:
  - name: no-gpu-ir-runs
    action: pipeline_run_ir
    when: "gpus > 0"
    effect: deny
```

## Not built yet

Inference-pipeline authoring, distributed/HPO lowering, remote-scheduler runs of IR-only models,
`exa pipeline show --ir` (use `explain`), Prefect deployment from an IR, recording the IR hash on
the MLflow run, and importing Snakemake/Nextflow. See ADR 0080 for the design intent.
