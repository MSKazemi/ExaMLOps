# Fine-Tuning / PEFT / Multi-LoRA (B7)

> Next-Gen 40 · feature **B7** · ADR 0044

B7 adds a fine-tuning workflow that produces **versioned, signed, eval-gated,
lineage-linked adapters**, and serves them **multi-LoRA** on a shared base with per-request
routing. Every adapter is a governed artifact, and the platform keeps its measured score and
the score someone typed in as separate things.

What this page describes is what runs today. The limits are listed under
[What is not built](#what-is-not-built).

## Train an adapter

```bash
exa finetune demo-base --train --dataset <A1-rev> --rank 4 --steps 80 --eval-floor 0.7
# Trained adapter demo-base-lora-<rev>: held_out_accuracy 0.8555 on 512 held-out samples
#   (baseline 0.7598, loss 0.6456 → 0.4401, 392 adapter parameters, base unchanged).
```

`--train` runs the shipped reference script `examlops.finetuning.train_lora`. The base stack
is frozen and only the rank-decomposed `A`/`B` factors are trained. The script then scores the
adapter on a held-out split that the training loop never draws from, and the adapter is
registered with **that** score (`eval_source=measured`). The run is deterministic under
`--seed` / `EXAMLOPS_SEED`. It resumes from its last verified checkpoint, and it exits
0 / 75 / 70 (done / retryable / fatal).

The reference task is **synthetic**, and the run record says so (`"data": "synthetic"`). The
script proves that the training, the scoring and the registration are real. It is not a
language-model fine-tune.

### Backends and methods

| `--backend` | `--method` | What trains |
|---|---|---|
| `torch-lora` (default) | `lora` | Built-in LoRA factors on the frozen reference stack |
| `torch-lora` | `qlora` | The same factors over a base rounded through bfloat16. This is **not** bitsandbytes NF4. |
| `torch-lora` | `full` | Every weight. The run produces a full model rather than a delta. |
| `peft` | `lora` | Hugging Face PEFT: `peft.get_peft_model(model, LoraConfig(r, lora_alpha, target_modules=["fc1","fc2"]))` |

The `peft` backend needs the optional extra:

```bash
pip install 'examlops[finetune]'   # torch + peft (peft brings transformers/accelerate)
```

Without `peft`, asking for `--backend peft` fails with a clear message. It never falls back to
training something else. The backend also refuses `qlora`, because PEFT's QLoRA needs a 4-bit
bitsandbytes base on a CUDA device. It likewise refuses to train when the library leaves any
non-adapter parameter trainable.

### Train on the scheduler (mock / Slurm / Flux)

```bash
exa finetune demo-base --train --dataset <rev> --scheduler --gpus 1 --partition gpu \
    --time-limit 02:00:00 --account proj123
```

`--scheduler` submits the same script as a job on the phase-23 scheduler that
`EXAMLOPS_HPC_SCHEDULER` selects, and uses the job rules shared by every platform job
(`examlops.scheduler_jobs`):

- a generated `run.sh` with mode 0700 and every value quoted
- no environment values written into the script
- the script kept out of the repository
- the job's `PYTHONPATH` led by the submitter's `examlops`

The job is recorded in `hpc_jobs`, so `exa hpc jobs` lists it. Its id is also stamped on the
adapter row (`hpc_job_id`).

Each attempt waits no longer than the run's timeout, and a job that does not finish in time is
cancelled. The supervision loop is the same as the local one: a retryable failure resubmits and
resumes from the last checkpoint, and a FATAL marker stops the run. Only resource keys on an
allow-list are accepted (`gpus`, `partition`, `time`, `account`, …), and each must be a single
token.

The run directory must be on storage that both the compute node and the submitting host can
reach, which is the shared-filesystem layout the platform deploys on.

### MLflow

When `MLFLOW_TRACKING_URI` is set, a completed run is logged to MLflow as one run in
experiment `finetune/<base>`. The run carries:

- the parameters
- the measured metrics
- provenance tags: `examlops.eval_source=measured`, the dataset revision, the adapter digest
  and the training run id
- the adapter bundle under `adapter/`

A **full** fine-tune is also registered as a normal **model version** named after the adapter.
The MLflow run id, artifact URI and model version are written to the adapter row.

Logging is best effort. With no tracker it reports `skipped`. With an unreachable tracker it
reports `failed` and gives the reason. In both cases the adapter stays registered in
`platform.db`, which is the system of record.

`--mlflow` makes a missing tracker count as a failure, and `--no-mlflow` turns logging off. A `--mlflow` run that
was not logged exits 1; the adapter stays registered.
HTTP calls are bounded at 30 s and two retries, unless MLflow's own `MLFLOW_HTTP_REQUEST_TIMEOUT` / `MLFLOW_HTTP_REQUEST_MAX_RETRIES` are already set.

### The adapter bundle

Every run writes `<run>/adapter/adapter_model.pt` and `adapter_config.json`. The config
records the backend, method, rank, alpha, seed, the frozen base's digest and the tensors'
digest.

Loading a bundle is **verify-before-load**. The tensors must hash to the digest the registry
recorded, or they are not loaded, and `torch.load` runs with `weights_only=True`.

## Register an adapter trained elsewhere

```bash
exa finetune llama3.1-8b --method lora --dataset <rev> --asserted-eval 0.82 \
    --adapter-uri /models/adapters/llama-sci-v3
```

Without `--train`, nothing is trained. A score you supply is stored as `asserted_eval_score`,
which is **unverified**. It can block a promotion, and it can never clear one.

`--adapter-uri` is the PEFT adapter directory on the **serving host**. vLLM loads it from
there, and this command only stores the path; it never opens it.

## Promotion (C3 eval-gate)

```bash
exa serve adapter promote <adapter>
```

Registering an existing adapter id again (a second `--train` on the same base, method and dataset
revision, or `exa serve adapter add` pointing at other weights) **demotes** it and clears its
training and MLflow provenance. A promotion belongs to the weights the gate saw.

An adapter whose measured score is below its floor is blocked, and so is an adapter that has
only an asserted score. `--accept-unverified` is a deliberate override, and it is audited.

## Multi-LoRA serving

```bash
# Routing only: no model runs (the default)
exa serve adapter route demo-base <adapter> --prompt "a b c"

# Real inference through the trained adapter, on CPU
exa serve adapter route demo-base <adapter> --engine torch --prompt "a b c"

# The E2 engine: a running `vllm serve <base> --enable-lora` with
# VLLM_ALLOW_RUNTIME_LORA_UPDATING=True
exa serve adapter route llama3.1-8b <adapter> --engine vllm --base-url http://gpu-node:8000
```

The router keeps one base and a bounded **LRU hot set** (`--hot-set`). It refuses an adapter
whose base ref differs from the serving base. Engines that actually serve inference (`torch`,
`vllm`) also enforce three more checks:

- **Promotion:** an unpromoted adapter is refused, and the refusal is audited.
  `--allow-unpromoted` overrides this, and the override is audited too.
- **Signature:** the HMAC covers the adapter's identity (id, base, dataset) and its weights (the
  tensor digest and the `adapter_uri` an engine loads). A row whose signature no longer matches
  (a different base, dataset, digest or location under a signed id) is refused. So is an
  **unsigned** row while a signing key is configured, and so is a row whose signature cannot be
  checked because the signer failed. Rows signed before the weights were bound into the signature
  fail this check; re-register them.
- **Loading is audited** (`adapter_loaded`).

The two engines differ in how they load and route adapters:

- **`torch`** builds the reference base once and keeps each resident adapter as a verified set
  of tensors. For each request it swaps that adapter's tensors into the shared base.
  - Two adapters give different answers to the same prompt, and each answer equals what the
    adapter computes when loaded by hand.
  - An adapter trained on different base **weights** is refused, even when the base name
    matches.
  - A full fine-tune is refused, because it is a model version and not an adapter.
- **`vllm`** loads an admitted adapter with `POST /v1/load_lora_adapter` (`lora_path` =
  the registered `adapter_uri`) and unloads an evicted one with
  `POST /v1/unload_lora_adapter`. It routes a request by sending the adapter id as the
  OpenAI-compatible `model`.

`examlops.finetuning.serving.render_lora_args(max_loras=…, max_lora_rank=…)` renders the
`vllm serve` flags. Set `max_loras` to the hot-set size.

```python
from examlops.finetuning import MultiLoRARouter
from examlops.finetuning.serving import TorchAdapterEngine

router = MultiLoRARouter("demo-base", hot_set_size=4, engine=TorchAdapterEngine())
router.route("adapter-a", "alpha beta")   # cold load, verified, served
router.stats()                            # hits / misses / evictions / loaded
```

## What is not built

- **No real LLM checkpoint ships**, and nothing here has run on a GPU. PEFT is exercised on the
  reference stack only.
- **TRL (`SFTTrainer`) is not used.** It needs a text dataset and a causal-LM base.
- **QLoRA on the PEFT path** (a bitsandbytes 4-bit base) is not built.
- **FSDP/DeepSpeed sharding of a full fine-tune** is not built. `--method full` trains the
  reference stack in a single process.
- **Only the router's own callers use it.** The `vllm` adapter engine has been tested against a
  stub server but not against a live vLLM. The gateway and Ray Serve do not yet send requests
  through the router.

## Related

- **A1** dataset revisions: adapters pin to a revision so they can be reproduced.
- **A2** lineage: each run records base + dataset → adapter.
- **C3** eval-gate: blocks promotion on missing or below-floor measured evidence.
- **D3** supply chain: adapters are HMAC-signed, and bundles are digest-verified before they load.
- **E2 / E6**: the serving engine and the scheduler path. See also
  [distributed training](distributed-training.md).
