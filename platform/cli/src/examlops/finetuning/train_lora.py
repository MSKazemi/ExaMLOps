"""Reference LoRA fine-tuning script — real training, a real held-out score, honest exit codes.

Run it directly (``examlops.finetuning.runner`` builds the command)::

    python -m examlops.finetuning.train_lora --run-dir RUN --steps 80

What it is: a small but genuine LoRA fine-tune. The base stack (:mod:`examlops.finetuning.lora`)
is frozen; only the rank-decomposed ``A``/``B`` factors are optimised; the score it reports is
**measured on a held-out split** the training loop never draws from. It is not a language-model
fine-tune and trains no ExaMLOps registry model — it is to ADR 0044 what ``train_ddp`` is to
ADR 0032: the shipped, runnable thing the platform's claims are anchored to.

Determinism: ``--seed`` (default ``EXAMLOPS_SEED``) fixes the base initialisation, the adapter
initialisation, the teacher coefficients and every batch, which is a function of
``(seed, split, index)`` alone. Two runs with the same seed produce the same adapter digest and
the same eval score, and a resumed run replays the batches an uninterrupted run would have used.

Checkpoints reuse the ADR 0032 integrity layer (:mod:`examlops.distributed.checkpoint_files`):
atomic writes, a manifest with per-file SHA-256, and resume from the newest checkpoint that
verifies — a corrupt one is skipped, never loaded.

Final line: ``EXAMLOPS_FINETUNE_METRICS=<json>``. Exit codes: 0 done, 75 recoverable (transient
I/O — rerun and it resumes), 70 fatal (bad config, non-finite loss, missing backend — do not).

Fault injection (tests/demo only): ``EXAMLOPS_FINETUNE_FAULT=recoverable|fatal`` makes the run
fail that way once at ``EXAMLOPS_FINETUNE_FAULT_STEP`` (default 1); a marker file in the run
directory stops it firing again after the rerun.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from examlops.distributed import checkpoint_files as cf
from examlops.finetuning import lora

#: The one line a supervisor parses. Distinct from the distributed script's marker so a log that
#: contains both is never mistaken for one run.
METRICS_MARKER = "EXAMLOPS_FINETUNE_METRICS="

DEFAULT_STEPS = 80
DEFAULT_BATCH = 64
DEFAULT_EVAL_BATCHES = 8
DEFAULT_LR = 0.05


class FatalTrainingError(Exception):
    """A failure a rerun would reproduce."""


def _log(msg: str) -> None:
    print(f"[finetune] {msg}", flush=True)


def config(args: argparse.Namespace, seed: int) -> dict[str, Any]:
    """The settings a checkpoint is only valid under (not the step budget — a longer run resumes)."""
    return {
        "stack": f"emb{lora.VOCAB}x{lora.DIM}-mlp{lora.HIDDEN}-{lora.CLASSES}",
        "backend": args.backend,
        "method": args.method,
        "rank": args.rank,
        "alpha": args.alpha,
        "lr": args.lr,
        "batch": args.batch,
        "seed": seed,
    }


def _save_checkpoint(
    torch: Any,
    backend: Any,
    model: Any,
    opt: Any,
    step: int,
    run_dir: Path,
    cfg_hash: str,
    resumed_from: int | None,
) -> None:
    """One file holding the adapter tensors + optimiser state, committed by a manifest."""
    buf = io.BytesIO()
    torch.save({"adapter": backend.adapter_state(model), "opt": opt.state_dict()}, buf)
    data = buf.getvalue()
    directory = cf.step_dir(run_dir, step)
    directory.mkdir(parents=True, exist_ok=True)
    fname = cf.shard_name(0, 1)
    cf.atomic_write_bytes(directory / fname, data)
    cf.write_manifest(
        run_dir,
        step=step,
        world_size=1,
        cfg_hash=cfg_hash,
        shards=[
            {
                "rank": 0,
                "file": fname,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        ],
        resumed_from_step=resumed_from,
    )


def _load_checkpoint(torch: Any, model: Any, opt: Any, status: cf.CheckpointStatus) -> None:
    shard = status.manifest["shards"][0]
    assert status.directory is not None
    blob = torch.load(status.directory / shard["file"], map_location="cpu", weights_only=True)
    named = dict(model.named_parameters())
    saved = blob["adapter"]
    if set(saved) != {n for n in named if "lora_" in n}:
        raise FatalTrainingError("checkpoint adapter parameters do not match this configuration")
    with torch.no_grad():
        for name, tensor in saved.items():
            named[name].copy_(tensor)
    opt.load_state_dict(blob["opt"])


def _maybe_inject_fault(run_dir: Path, step: int) -> None:
    kind = os.getenv("EXAMLOPS_FINETUNE_FAULT", "").strip().lower()
    if not kind or step != int(os.getenv("EXAMLOPS_FINETUNE_FAULT_STEP", "1") or 1):
        return
    try:
        fd = os.open(run_dir / "fault.fired", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return
    os.close(fd)
    if kind == "fatal":
        raise FatalTrainingError(f"fault injection: fatal at step {step}")
    raise OSError(f"fault injection: simulated transient failure at step {step}")


def evaluate(torch: Any, model: Any, seed: int, batch: int, batches: int) -> dict[str, Any]:
    """Accuracy and loss on the held-out split — the only number allowed to be called *measured*."""
    model.eval()
    correct = total = 0
    loss_sum = 0.0
    with torch.no_grad():
        for i in range(batches):
            x, y = lora.make_batch(torch, seed, lora.EVAL, i, batch)
            logits = model(x)
            loss_sum += float(torch.nn.functional.cross_entropy(logits, y)) * y.numel()
            correct += int((logits.argmax(dim=1) == y).sum())
            total += int(y.numel())
    model.train()
    return {
        "eval_metric": "held_out_accuracy",
        "eval_score": correct / total,
        "eval_loss": loss_sum / total,
        "eval_n": total,
    }


def train(args: argparse.Namespace) -> int:
    import torch

    # The run directory is created *before* the configuration is validated, so that a fatal
    # config error still has somewhere to leave its FATAL marker — without it the supervisor
    # would retry a failure that can only fail again.
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.steps <= 0 or args.checkpoint_every <= 0 or args.batch <= 0:
        raise FatalTrainingError("--steps, --checkpoint-every and --batch must be positive")
    if args.rank < 1:
        raise FatalTrainingError("--rank must be >= 1")
    seed = args.seed if args.seed is not None else int(os.getenv("EXAMLOPS_SEED", "0") or 0)
    torch.set_num_threads(1)

    try:
        backend = lora.get_backend(args.backend)
        model = backend.build(seed=seed, rank=args.rank, alpha=args.alpha, method=args.method)
    except lora.BackendNotAvailable as exc:
        raise FatalTrainingError(str(exc)) from exc

    base_sha = _base_sha256(model)
    trainable = lora.trainable_parameters(model)
    if not trainable:
        raise FatalTrainingError("no trainable adapter parameters — nothing would be fine-tuned")
    opt = torch.optim.Adam(trainable, lr=args.lr)
    cfg_hash = cf.config_hash(config(args, seed))

    latest, skipped = cf.find_latest_valid(run_dir, cfg_hash)
    skipped_info = [{"step": s.step, "reason": s.reason} for s in skipped]
    resumed_from: int | None = None
    start = 0
    if latest is not None:
        _load_checkpoint(torch, model, opt, latest)
        resumed_from = start = latest.step
        _log(f"resumed from step {resumed_from} (skipped invalid: {skipped_info})")
    else:
        _log(f"starting at step 0 (skipped invalid: {skipped_info})")

    baseline = evaluate(torch, model, seed, args.batch, DEFAULT_EVAL_BATCHES)
    first_loss = last_loss = None
    for step in range(start, args.steps):
        _maybe_inject_fault(run_dir, step)
        x, y = lora.make_batch(torch, seed, lora.TRAIN, step, args.batch)
        opt.zero_grad(set_to_none=True)
        loss = torch.nn.functional.cross_entropy(model(x), y)
        loss.backward()
        opt.step()
        val = float(loss.detach())
        if not math.isfinite(val):
            raise FatalTrainingError(f"non-finite loss at step {step}")
        first_loss = val if first_loss is None else first_loss
        last_loss = val
        done = step + 1
        if done % args.checkpoint_every == 0 or done == args.steps:
            _save_checkpoint(torch, backend, model, opt, done, run_dir, cfg_hash, resumed_from)

    measured = evaluate(torch, model, seed, args.batch, DEFAULT_EVAL_BATCHES)
    state = backend.adapter_state(model)
    metrics = {
        "status": "complete",
        "backend": backend.name,
        "method": args.method,
        "rank": args.rank,
        "alpha": args.alpha,
        "steps": args.steps,
        "steps_run": args.steps - start,
        "batch": args.batch,
        "lr": args.lr,
        "seed": seed,
        "config_hash": cfg_hash,
        "resumed_from_step": resumed_from,
        "skipped_checkpoints": skipped_info,
        "first_loss": first_loss,
        "final_loss": last_loss,
        "baseline_eval_score": baseline["eval_score"],
        "trainable_parameters": sum(p.numel() for p in trainable),
        "base_parameters": sum(p.numel() for p in model.parameters())
        - sum(p.numel() for p in trainable),
        "base_weights_sha256": base_sha,
        "base_weights_unchanged": base_sha == _base_sha256(model),
        "adapter_sha256": lora.adapter_sha256(state),
        "data": "synthetic",
        **measured,
    }
    print(METRICS_MARKER + json.dumps(metrics, sort_keys=True), flush=True)
    return cf.EXIT_OK


def _base_sha256(model: Any) -> str:
    """Digest of the frozen parameters, so a run can prove it did not move the base."""
    h = hashlib.sha256()
    for name, p in sorted(model.named_parameters()):
        if "lora_" in name:
            continue
        h.update(name.encode())
        h.update(p.detach().cpu().contiguous().float().numpy().tobytes())
    return h.hexdigest()


def parse_metrics(text: str) -> dict[str, Any] | None:
    """The last ``EXAMLOPS_FINETUNE_METRICS=<json>`` line in ``text``, or None."""
    found = None
    for line in text.splitlines():
        if line.startswith(METRICS_MARKER):
            try:
                found = json.loads(line[len(METRICS_MARKER) :])
            except ValueError:
                continue
    return found


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    ap.add_argument("--checkpoint-every", type=int, default=20)
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--rank", type=int, default=4)
    ap.add_argument("--alpha", type=float, default=16.0)
    ap.add_argument("--lr", type=float, default=DEFAULT_LR)
    ap.add_argument("--method", default="lora", choices=("lora", "qlora"))
    ap.add_argument("--backend", default=lora.DEFAULT_BACKEND)
    ap.add_argument("--seed", type=int, default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return train(args)
    except FatalTrainingError as exc:
        _log(f"FATAL: {exc}")
        try:
            cf.atomic_write_bytes(
                Path(args.run_dir) / cf.FATAL_MARKER, json.dumps({"error": str(exc)}).encode()
            )
        except OSError:
            pass
        return cf.EXIT_FATAL
    except ImportError as exc:
        _log(f"FATAL: torch is required to fine-tune and is not importable: {exc}")
        return cf.EXIT_FATAL
    except (RuntimeError, OSError, TimeoutError, ConnectionError) as exc:
        _log(f"recoverable failure: {type(exc).__name__}: {exc}")
        return cf.EXIT_RECOVERABLE


if __name__ == "__main__":
    sys.exit(main())
