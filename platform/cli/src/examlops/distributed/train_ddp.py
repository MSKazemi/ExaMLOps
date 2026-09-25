"""Reference distributed training script — DistributedDataParallel + sharded, resumable checkpoints.

``--strategy fsdp`` runs the same job under PyTorch FSDP2 (``fully_shard``) instead of DDP.

Run it under ``torchrun`` (``examlops.distributed.launch`` builds the command)::

    torchrun --standalone --nproc-per-node=2 train_ddp.py --run-dir RUN --steps 10

What it is: a small, real DDP job (tiny MLP, synthetic regression data) that exercises everything
ADR 0032 decision 2 asks of the *training side* — periodic **sharded checkpoints** (each rank writes
its own shard file, rank 0 commits a manifest with per-shard SHA-256), **resume from the newest
checkpoint that verifies** (a corrupt or missing shard makes that step invalid and it falls back to
the previous one), deterministic seeding, and honest exit codes. It implements DDP and FSDP2 (not
DeepSpeed/Megatron) and trains no ExaMLOps registry model: a registry model supplies its own
entrypoint module that honours the same argument contract (see ``examlops.distributed.strategy``).

Determinism: the model is initialised from the seed on every rank; the batch for (step, rank) is
generated from ``seed, step, rank`` alone, so there is no data-loader state to checkpoint and a
resumed run replays exactly the batches an uninterrupted run would have used.

Final line on rank 0: ``EXAMLOPS_DIST_METRICS=<json>``. Exit codes: 0 done, 75 recoverable
(peer killed / collective broke — resubmit), 70 fatal (bad config, non-finite loss — do not).

Fault injection (tests only): ``EXAMLOPS_DIST_FAULT_STEP=N`` makes rank ``EXAMLOPS_DIST_FAULT_RANK``
(default 1) SIGKILL itself once, at the start of step N; a marker file in the run directory keeps it
from firing again after the resume.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import signal
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from examlops.distributed import checkpoint_files as cf

IN_DIM, HIDDEN = 16, 32
BATCH_PER_RANK = 32
LR, MOMENTUM = 0.005, 0.9
STEPS_PER_EPOCH = 4
#: Parallelism strategies this script implements. The config hash deliberately excludes the
#: strategy: shards hold whole tensors, so a DDP checkpoint resumes under FSDP and vice versa.
STRATEGIES = ("ddp", "fsdp")


class FatalTrainingError(Exception):
    """A failure a resubmission would reproduce."""


def _log(rank: int, msg: str) -> None:
    print(f"[rank {rank}] {msg}", flush=True)


def _config(seed: int) -> dict[str, Any]:
    return {
        "model": f"mlp-{IN_DIM}-{HIDDEN}-1",
        "lr": LR,
        "momentum": MOMENTUM,
        "batch_per_rank": BATCH_PER_RANK,
        "seed": seed,
    }


def _batch(torch, seed: int, step: int, rank: int, device):
    g = torch.Generator().manual_seed(seed * 1_000_003 + step * 977 + rank)
    x = torch.randn(BATCH_PER_RANK, IN_DIM, generator=g)
    wg = torch.Generator().manual_seed(seed + 17)
    w_true = torch.randn(IN_DIM, 1, generator=wg)
    y = x @ w_true + 0.01 * torch.randn(BATCH_PER_RANK, 1, generator=g)
    return x.to(device), y.to(device)


def _full(t):
    """The whole tensor behind ``t``: an FSDP ``DTensor`` is gathered (a collective — every rank
    must call this for every parameter, in the same order); a plain tensor is itself."""
    full = getattr(t, "full_tensor", None)
    return full() if callable(full) else t


def _save_shard(torch, model, opt, rank: int, world: int, directory: Path) -> dict[str, Any]:
    """This rank's slice: parameters (and momentum buffers) whose index % world == rank.

    Under FSDP every parameter is first gathered to its full shape on every rank (``_full``), so a
    shard holds whole tensors whatever the strategy was and a checkpoint written under one strategy
    or world size reassembles under another.
    """
    params, mom = {}, {}
    for i, (name, p) in enumerate(model.named_parameters()):
        whole = _full(p.detach())
        buf = opt.state.get(p, {}).get("momentum_buffer")
        whole_buf = _full(buf) if buf is not None else None
        if i % world != rank:
            continue
        params[name] = whole.cpu().clone()
        if whole_buf is not None:
            mom[name] = whole_buf.detach().cpu().clone()
    buf_io = io.BytesIO()
    torch.save({"params": params, "momentum": mom}, buf_io)
    data = buf_io.getvalue()
    directory.mkdir(parents=True, exist_ok=True)
    fname = cf.shard_name(rank, world)
    cf.atomic_write_bytes(directory / fname, data)
    return {
        "rank": rank,
        "file": fname,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _load_checkpoint(torch, model, opt, status: cf.CheckpointStatus) -> None:
    """Reassemble the full state from every shard (whatever the current world size is)."""
    params: dict[str, Any] = {}
    mom: dict[str, Any] = {}
    for s in status.manifest["shards"]:
        blob = torch.load(status.directory / s["file"], map_location="cpu", weights_only=True)
        params.update(blob["params"])
        mom.update(blob["momentum"])
    named = dict(model.named_parameters())
    if set(params) != set(named):
        raise RuntimeError("checkpoint parameter set does not match the model")
    with torch.no_grad():
        for name, p in named.items():
            mesh = getattr(p, "device_mesh", None)
            if mesh is not None:  # FSDP: re-shard the whole tensor onto this parameter's layout
                from torch.distributed.tensor import distribute_tensor

                p.copy_(distribute_tensor(params[name].to(p.device), mesh, p.placements))
                if name in mom:
                    opt.state[p]["momentum_buffer"] = distribute_tensor(
                        mom[name].to(p.device), mesh, p.placements
                    )
                continue
            p.copy_(params[name])
            if name in mom:
                opt.state[p]["momentum_buffer"] = mom[name].to(p.device)


def _weights_sha(model) -> str:
    """Digest of the full weights. Collective under FSDP: every rank must call it."""
    h = hashlib.sha256()
    for _, p in model.named_parameters():
        h.update(_full(p.detach()).cpu().contiguous().numpy().tobytes())
    return h.hexdigest()


def _wrap(torch, model, strategy: str, use_cuda: bool, local_rank: int):
    """Apply the parallelism strategy; returns the module to call forward/backward on.

    ``ddp`` — DistributedDataParallel (replicated weights, all-reduced gradients).
    ``fsdp`` — PyTorch FSDP2 (``fully_shard``): each Linear and then the root are sharded, so a rank
    holds 1/world of every parameter and its optimizer state (ZeRO-3-style); gradients are
    reduce-scattered. Works on CPU/gloo and on CUDA/NCCL through the same call.
    """
    if strategy == "ddp":
        from torch.nn.parallel import DistributedDataParallel as DDP

        return DDP(model, device_ids=[local_rank] if use_cuda else None)
    if strategy == "fsdp":
        try:
            from torch.distributed.fsdp import fully_shard
        except ImportError as exc:  # torch < 2.6
            raise FatalTrainingError(f"FSDP2 (fully_shard) is not available: {exc}") from exc
        for layer in model:
            if isinstance(layer, torch.nn.Linear):
                fully_shard(layer)
        fully_shard(model)
        return model
    raise FatalTrainingError(
        f"strategy {strategy!r} is not implemented by this script (use ddp or fsdp)"
    )


def _maybe_inject_fault(run_dir: Path, step: int, rank: int) -> None:
    raw = os.getenv("EXAMLOPS_DIST_FAULT_STEP", "").strip()
    if not raw or int(raw) != step or rank != int(os.getenv("EXAMLOPS_DIST_FAULT_RANK", "1")):
        return
    try:
        fd = os.open(run_dir / "fault.fired", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return
    os.close(fd)
    _log(rank, f"fault injection: SIGKILL at start of step {step}")
    os.kill(os.getpid(), signal.SIGKILL)


def train(args: argparse.Namespace) -> int:
    import torch
    import torch.distributed as dist

    if args.steps <= 0 or args.checkpoint_every <= 0:
        raise FatalTrainingError("--steps and --checkpoint-every must be positive")
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    seed = args.seed if args.seed is not None else int(os.getenv("EXAMLOPS_SEED", "0") or 0)

    rank = int(os.environ.get("RANK", "0"))
    world = int(os.environ.get("WORLD_SIZE", "1"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    use_cuda = torch.cuda.is_available()
    backend = "nccl" if use_cuda else "gloo"
    if use_cuda:
        torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank) if use_cuda else torch.device("cpu")
    torch.set_num_threads(1)
    dist.init_process_group(backend=backend, timeout=timedelta(seconds=120))
    try:
        torch.manual_seed(seed)
        model = torch.nn.Sequential(
            torch.nn.Linear(IN_DIM, HIDDEN), torch.nn.Tanh(), torch.nn.Linear(HIDDEN, 1)
        ).to(device)
        ddp = _wrap(torch, model, args.strategy, use_cuda, local_rank)
        opt = torch.optim.SGD(ddp.parameters(), lr=LR, momentum=MOMENTUM)
        cfg_hash = cf.config_hash(_config(seed))

        # Rank 0 chooses the checkpoint so every rank loads the same one.
        pick: list[Any] = [None, []]
        if rank == 0:
            latest, skipped = cf.find_latest_valid(run_dir, cfg_hash)
            pick = [
                latest.step if latest else None,
                [{"step": s.step, "reason": s.reason} for s in skipped],
            ]
        dist.broadcast_object_list(pick, src=0)
        resumed_from: int | None = pick[0]
        skipped_info: list[dict[str, Any]] = pick[1]
        start = 0
        if resumed_from is not None:
            status = cf.verify_checkpoint_dir(cf.step_dir(run_dir, resumed_from), cfg_hash)
            if not status.valid:
                raise RuntimeError(f"checkpoint {resumed_from} stopped verifying: {status.reason}")
            _load_checkpoint(torch, model, opt, status)
            start = resumed_from
            _log(rank, f"resumed from step {resumed_from} (skipped invalid: {skipped_info})")
        else:
            _log(rank, f"no valid checkpoint; starting at step 0 (skipped invalid: {skipped_info})")
        dist.barrier()

        first_loss = last_loss = None
        for s in range(start, args.steps):
            _maybe_inject_fault(run_dir, s, rank)
            x, y = _batch(torch, seed, s, rank, device)
            opt.zero_grad(set_to_none=True)
            loss = torch.nn.functional.mse_loss(ddp(x), y)
            loss.backward()  # DDP all-reduces (averages) the gradients here
            opt.step()
            mean = loss.detach().clone()
            dist.all_reduce(mean)
            val = float(mean) / world
            if not math.isfinite(val):
                raise FatalTrainingError(f"non-finite loss at step {s}")
            first_loss = val if first_loss is None else first_loss
            last_loss = val
            done = s + 1
            if done % args.checkpoint_every == 0 or done == args.steps:
                d = cf.step_dir(run_dir, done)
                mine = _save_shard(torch, model, opt, rank, world, d)
                gathered: list[Any] = [None] * world
                dist.all_gather_object(gathered, mine)
                if rank == 0:
                    cf.write_manifest(
                        run_dir,
                        step=done,
                        world_size=world,
                        cfg_hash=cfg_hash,
                        shards=gathered,
                        resumed_from_step=resumed_from,
                        epoch=done // STEPS_PER_EPOCH,
                    )
                dist.barrier()
        weights_sha = _weights_sha(model)  # collective under FSDP: every rank computes it
        if rank == 0:
            metrics = {
                "status": "complete",
                "strategy": args.strategy,
                "steps": args.steps,
                "steps_run": args.steps - start,
                "world_size": world,
                "backend": backend,
                "seed": seed,
                "config_hash": cfg_hash,
                "resumed_from_step": resumed_from,
                "skipped_checkpoints": skipped_info,
                "first_loss": first_loss,
                "final_loss": last_loss,
                "weights_sha256": weights_sha,
            }
            print(cf.METRICS_MARKER + json.dumps(metrics, sort_keys=True), flush=True)
        return cf.EXIT_OK
    finally:
        try:
            dist.destroy_process_group()
        except Exception:  # noqa: BLE001 - teardown after a peer died must not mask the cause
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--steps", type=int, default=12)
    ap.add_argument("--checkpoint-every", type=int, default=4)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--strategy", choices=STRATEGIES, default="ddp")
    args = ap.parse_args(argv)
    rank = int(os.environ.get("RANK", "0"))
    try:
        return train(args)
    except FatalTrainingError as exc:
        _log(rank, f"FATAL: {exc}")
        try:
            cf.atomic_write_bytes(
                Path(args.run_dir) / cf.FATAL_MARKER, json.dumps({"error": str(exc)}).encode()
            )
        except OSError:
            pass
        return cf.EXIT_FATAL
    except (RuntimeError, OSError, TimeoutError, ConnectionError) as exc:
        _log(rank, f"recoverable failure: {type(exc).__name__}: {exc}")
        return cf.EXIT_RECOVERABLE


if __name__ == "__main__":
    sys.exit(main())
