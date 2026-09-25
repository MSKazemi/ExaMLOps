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
  pipeline, are the placement ask. `Resources` *is* `examlops.hpc_placement.ResourceAsk` — the
  class `--cluster auto` placement scores — not a copy of it. Both lower into the YAML's
  `placement:` section (below) and default `--cluster` / `--gpus` at run time.
* Dataset order is the order you pass them to `train`; it is the run order and is part of the hash.

## Compile, explain, run

```bash
exa pipeline compile flows/jpcp.py --out jpcp.ir.json          # print / write the IR + hash
exa pipeline compile flows/jpcp.py --out jpcp.ir.json --yaml jpcp.yaml   # also lower to YAML
exa pipeline compile flows/jpcp.py -o jpcp.yaml                # the registry YAML *as* the IR
exa pipeline explain jpcp.ir.json                              # read-only topological plan
exa pipeline explain usecases/reference/models/jpcp.yaml       # a registry YAML is an IR too
exa pipeline show JPCP                                         # a pack model's IR, by name
exa pipeline show JPCP --ir                                    # ... as the JSON graph + hash
exa pipeline run --ir jpcp.ir.json --dummy                     # train through the real generator
exa pipeline run --ir jpcp.yaml --dummy                        # same, from the YAML form
exa pipeline decompile models/jpcp.yaml --out flows/jpcp.py    # the reverse: YAML -> DSL source
```

`compile` exits 1 on a validation error, on an unlowerable pipeline when a YAML is to be written
(`--yaml`, or an `--out` ending in `.yaml`/`.yml`), or when policy denies it. `--out` and `--yaml` write files; the IR is deterministic, so the same pipeline
always produces the same `content_hash`, and editing the JSON afterwards is caught on load.

## Adopt an existing YAML: `exa pipeline decompile`

YAML authoring stays first-class, so most models never need this. When you *do* want to move one
into typed Python, `decompile` writes the twin for you instead of you retyping it:

```bash
exa pipeline decompile usecases/reference/models/jpcp.yaml --out flows/jpcp.py
exa pipeline compile flows/jpcp.py --yaml /tmp/jpcp.yaml   # same model, byte-for-byte equivalent
```

The emitted file is an *exact* twin — compiling it lowers back to a YAML that loads to the same
`ModelYAMLConfig`. That is not a hope: `decompile` builds the IR, lowers it with the production
lowering, and compares the result with the YAML it read before emitting anything.

It therefore **refuses rather than writes** whenever the YAML holds something the DSL cannot carry,
naming the construct: a top-level key that is neither a train-step param nor a registry section, a
dataset key the `dataset` step has no param for, a model with no `datasets`, a missing
`config_class`/`task_type`, or a value YAML parsed into a non-JSON type (an *unquoted* date is the
common one — quote it). A file that silently dropped a section would be worse than no file.

`--force` overwrites an existing `--out`; without it an existing file is left alone. With no
`--out` the source goes to stdout, and `--json` returns `{name, content_hash, steps, source, …}`.

## The IR

**The per-model registry YAML is the IR** (ADR 0080 decision 2): it is what is reviewed, committed
and run. Every IR surface accepts it — `explain FILE.yaml`, `run --ir FILE.yaml`, `show NAME`
reads it from the pack, and `compile -o FILE.yaml` writes it. The JSON graph below is the same
pipeline in a form that also keeps step ids, typed ports and a content hash; a YAML is turned into
it by the decompiler's two gates (every key placed, and the graph lowers back to the identical
mapping), so a command given either form behaves identically. Files over 4 MiB are refused before
parsing.

### The `placement:` section

```yaml
placement:
  cluster: auto   # an ACTIVE cluster name, or 'auto' (placement picks, ADR 0077)
  gpus: 2         # the train step's ask — hpc_placement.ResourceAsk
  cpus: 8
  nodes: 1
```

Every key is optional; the section is what the train step's `resources=` and the pipeline's
`cluster=` lower to, so nothing about a training pipeline is dropped by lowering any more (only
`resources=` on a non-train step, which the flow never schedules as its own job, is reported as
not carried). It is validated fail-closed: an unknown key, a bool/negative/non-integer count,
`nodes: 0`, an empty cluster name or an absurd count (above 65 536 GPUs) is an error. An empty
`placement: {}` is refused by `decompile` (it would not round-trip).

`exa pipeline run --model NAME` uses a pack model's `placement.cluster` (and `gpus`) as the
default for `--cluster`/`--gpus`, the same way `run --ir` uses the IR's target. The whole ask —
`gpus`, `cpus` and `nodes` — is what `--cluster auto` placement scores, not only the GPU count;
an explicit `--gpus` replaces just the GPU count, and `--hardware-profile` replaces the whole ask.
An explicit `--cluster` always wins, a model with no section behaves exactly as before, and an
invalid section — or a model YAML that does not parse — stops the run with exit 1 rather than
being ignored.

The same ask is also what a Slurm/Flux job *requests*: `gpus` → the scheduler's GPU count,
`cpus` → `cpus_per_task`, `nodes` → `nodes`, so a job placed for its GPUs is submitted asking for
them. Explicit `EXAMLOPS_HPC_GPUS`/`_CPUS`/`_NODES` (or the `EXAMLOPS_SLURM_*` fallbacks) still
win over the section; an invalid section fails the submission instead of being dropped.

### The JSON graph

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
a second train step, a missing evaluate step, an evaluate split other than `validation`, and a
train-step ask the `placement:` section cannot hold (e.g. `nodes=0`).

**Slurm and Flux.** `run --ir` runs on every scheduler the platform supports, not only the inline
mock one. A Slurm/Flux compute node re-loads models from the use-case pack and would not otherwise
know an IR-only model, so the generator stages the lowered YAML into the job's working directory
through the scheduler adapter's own transport (`executor.put` — a copy on a shared filesystem,
SFTP over SSH) and the job runs `slurm_train_script.py --model-yaml <staged file>`, which registers
it before resolving the model. The node refuses (exit 1) a staged file that is missing or that
defines a different model than the job asked for. The `config_class` shim must still exist in the
node's pack — the same requirement a YAML-authored model has. Models from the pack stage nothing.

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

Inference-pipeline authoring, Prefect deployment from an IR, recording the IR hash on the MLflow run, and
importing Snakemake/Nextflow. See ADR 0080 for the design intent.

`hpo` and `custom_python` validate, compile and `explain`, and are **refused** at lowering and at
run — never skipped. They are refused because there is nothing to lower them *onto*, and the
refusal now says which thing is missing:

* **`hpo`** — nothing in the platform searches a hyper-parameter space. `exa pipeline hpo
  start|status|record` records a study row and dispatches **one** baseline training run through
  the control plane; the trials come from an optimiser outside ExaMLOps and are reported back with
  `hpo record`. No optimiser is a platform dependency (the pinned extra is `ray[serve]`, not
  `ray[tune]`; optuna is in no platform manifest) and `training_flow` trains once per run, with no
  trial loop and no reader of `search_space`. Lowering it would mean building that search driver.
* **`custom_python`** — there is nowhere to put the entrypoint. The registry YAML has no field for
  user code, `training_flow` is a fixed task sequence with no seam for an extra task, and the HPC
  compute node re-loads the use-case pack rather than this pipeline file, so an entrypoint string
  would have nothing to resolve against there.
