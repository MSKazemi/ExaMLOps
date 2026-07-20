# Fine-Tuning / PEFT / Multi-LoRA (B7)

> Next-Gen 40 · feature **B7** · ADR 0044 · spec `design/vision/specs/B7-fine-tuning-peft-lora.md`

B7 adds a fine-tuning workflow for LLMs that produces **versioned, signed, eval-gated,
lineage-linked adapters**, and serves many of them **multi-LoRA** on a single shared base
with per-request routing. Every adapter is a first-class, governed artifact — not a loose
file.

The reference path fine-tunes with HF PEFT/TRL on the scheduler (E6 for large jobs) and
serves through the E2 engine. This implementation is **pure Python**: adapter registration,
the eval-gate, the base-mismatch guard, and the LRU multi-LoRA routing all work — and are
fully testable — with no GPU, PEFT, or serving engine present.

## Fine-tune a base

```bash
exa finetune llama3.1-8b \
    --method lora --dataset <A1-rev> \
    --rank 8 --target-modules q_proj,v_proj \
    --eval 0.82 --eval-floor 0.75 --cost 3.5
# Registered adapter llama3.1-8b-lora-<rev> (lora, rank 8) on base llama3.1-8b
```

Methods: `lora`, `qlora`, `full` (full fine-tune has no rank). The dataset must be an **A1
revision** so the adapter is reproducible. The adapter is **signed** with the D3 HMAC key
(set `EXAMLOPS_SIGNING_KEY`; degrades to unsigned+marked otherwise), **lineage-linked**
(A2: base + dataset → adapter), and its cost is recorded.

## The adapter registry

Each adapter records its base ref, method, rank, training dataset revision, eval score, and
signature:

```bash
exa serve adapter list --base llama3.1-8b
# Adapter                   Base          Method  Rank  Eval   Promoted  Signed
# llama3.1-8b-lora-<rev>    llama3.1-8b   lora    8     0.820  no        yes
```

## Eval-gate before promotion (C3)

An adapter below its recorded quality floor **cannot** be promoted — the C3 gate blocks it:

```bash
exa serve adapter promote llama3.1-8b-lora-<rev>
# ✗ adapter … eval 0.72 < floor 0.75 — C3 gate blocks promotion   (exit 1)
```

Only an adapter at or above its floor promotes (and the block/allow is audited, D4).

## Multi-LoRA serving

One base is loaded **once**; requests select their adapter by id. A bounded **LRU hot set**
keeps memory in check, evicting the least-recently-used adapter when full:

```bash
exa serve adapter route llama3.1-8b llama3.1-8b-lora-<rev> --prompt "summarize…" --hot-set 4
# [llama3.1-8b-lora-<rev>] summarize…
#   loaded: llama3.1-8b-lora-<rev>
```

```python
from examlops.finetuning import MultiLoRARouter

router = MultiLoRARouter("llama3.1-8b", hot_set_size=4)
router.route("adapter-a", "…")   # cold load
router.route("adapter-b", "…")   # cold load
router.route("adapter-a", "…")   # hot hit
router.hit_rate                   # observability
```

## Base-mismatch refusal (safety)

Serving an adapter on a base **other than** the one it was trained on is refused — a LoRA
delta is only valid against its base:

```python
router = MultiLoRARouter("llama3.1-8b")   # wrong base
router.serve("adapter-trained-on-mistral")   # raises BaseMismatchError
```

## Graceful degradation

No HF PEFT/TRL, GPU, or serving engine is required to register adapters, enforce the
eval-gate, exercise routing + LRU eviction, or record cost/lineage. In production the same
`finetune` runs on the scheduler and the same routing contract drives the E2 multi-LoRA
engine.

## Related

- **A1** dataset revisions — adapters pin to a revision for reproducibility.
- **A2** lineage — base + dataset → adapter is recorded.
- **C3** eval-gate — blocks promotion of below-floor adapters.
- **D3** supply-chain — adapters are signed with the platform HMAC key.
- **E2 / E6** — serving engine + distributed training for the production path.
