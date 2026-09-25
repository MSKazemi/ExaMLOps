"""Per-model distributed-training plan: the model YAML's ``distributed:`` block (ADR 0032 dec. 1/3/4).

The model YAML is the single source of truth for a model's config (Phase 14). An optional::

    distributed:
      strategy: fsdp            # ddp | fsdp (default) | zero | megatron
      nodes: 2                  # max nodes (torchrun --nnodes=[min_nodes:]nodes)
      min_nodes: 1              # < nodes ⇒ torch-elastic: re-rendezvous when a node leaves/joins
      gpus_per_node: 4          # 0 = CPU (gloo)
      nproc_per_node: 4         # default: gpus_per_node, else 1
      max_restarts: 2           # torchrun in-job restarts before the job itself fails
      max_attempts: 3           # scheduler (re)submissions before giving up
      steps: 1000
      checkpoint_every: 50
      entrypoint: my_pack.train_dist   # module run with `torchrun -m`; default = reference script
      nccl:                     # surfaced into every rank's environment
        NCCL_IB_DISABLE: 1
        NCCL_SOCKET_IFNAME: ib0

selects the parallelism strategy, topology and elasticity for ``exa pipeline run --distributed`` and
``exa pipeline distributed run --model``. Like :mod:`examlops.hardware_profiles_yaml` it reads the
YAML through ``examlops.usecase.models_dir`` (the platform core never imports the pipeline engine),
unknown keys are errors (a typo'd ``min-nodes:`` must not silently disable elasticity), and it is
additive: no block ⇒ the defaults below.

:func:`preflight` is the fail-fast diagnostic the ADR's Consequences ask for: it refuses a plan the
environment cannot run *before* anything is submitted — ``zero`` without DeepSpeed, ``megatron``
without Megatron-Core, either without a model entrypoint (the reference script implements DDP and
FSDP only), NCCL settings on a CPU plan, or an entrypoint module that does not import.
"""

from __future__ import annotations

import importlib.util
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

STRATEGIES = ("ddp", "fsdp", "zero", "megatron")
DEFAULT_STRATEGY = "fsdp"
#: Strategies the shipped reference script implements; the others need a model ``entrypoint``.
REFERENCE_STRATEGIES = ("ddp", "fsdp")
#: The optional third-party package each strategy needs beyond torch.
_STRATEGY_PACKAGE = {"zero": "deepspeed", "megatron": "megatron.core"}

_INT_KEYS = (
    "nodes",
    "min_nodes",
    "gpus_per_node",
    "nproc_per_node",
    "max_restarts",
    "max_attempts",
    "steps",
    "checkpoint_every",
)
KEYS: frozenset[str] = frozenset({"strategy", "entrypoint", "nccl", *_INT_KEYS})
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_NCCL_KEY_RE = re.compile(r"^(NCCL|TORCH_NCCL)_[A-Z0-9_]+$")
_SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASS|CRED)", re.I)
_NCCL_VALUE_RE = re.compile(r"^[A-Za-z0-9_.,:/=^+-]{0,256}$")


class StrategyUnavailable(RuntimeError):
    """The requested plan cannot run in this environment (raised before anything is submitted)."""


@dataclass
class DistributedPlan:
    model: str
    strategy: str = DEFAULT_STRATEGY
    nodes: int = 1
    min_nodes: int | None = None
    gpus_per_node: int = 0
    nproc_per_node: int = 1
    max_restarts: int = 0
    max_attempts: int = 3
    steps: int = 12
    checkpoint_every: int = 4
    entrypoint: str | None = None
    nccl: dict[str, str] = field(default_factory=dict)
    source: str = "defaults"  # defaults | yaml | yaml+flags | flags

    @property
    def elastic(self) -> bool:
        return self.min_nodes is not None and self.min_nodes < self.nodes

    @property
    def nnodes_spec(self) -> str:
        return f"{self.min_nodes}:{self.nodes}" if self.elastic else str(self.nodes)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["elastic"] = self.elastic
        d["nnodes"] = self.nnodes_spec
        return d


def validate_block(block: Any) -> list[str]:
    """Errors in a YAML ``distributed:`` block (empty list = valid). Structural only."""
    if block in (None, {}):
        return []
    if not isinstance(block, dict):
        return ["distributed must be a mapping"]
    errors = [f"unknown distributed key {k!r}" for k in block if k not in KEYS]
    for key in _INT_KEYS:
        if key in block:
            v = block[key]
            if isinstance(v, bool) or not isinstance(v, int):
                errors.append(f"distributed.{key} must be an integer")
            elif v < (0 if key in ("gpus_per_node", "max_restarts") else 1):
                errors.append(f"distributed.{key} is out of range ({v})")
    if "strategy" in block and block["strategy"] not in STRATEGIES:
        errors.append(
            f"distributed.strategy must be one of {STRATEGIES}, got {block['strategy']!r}"
        )
    if "entrypoint" in block:
        ep = block["entrypoint"]
        if not isinstance(ep, str) or not _MODULE_RE.match(ep):
            errors.append("distributed.entrypoint must be a dotted Python module name")
    nodes, min_nodes = block.get("nodes", 1), block.get("min_nodes")
    if isinstance(nodes, int) and isinstance(min_nodes, int) and min_nodes > nodes:
        errors.append(f"distributed.min_nodes ({min_nodes}) exceeds nodes ({nodes})")
    errors += validate_nccl(block.get("nccl"))
    return errors


def validate_nccl(nccl: Any) -> list[str]:
    """NCCL settings are configuration, never secrets: names are allow-listed, values restricted."""
    if nccl in (None, {}):
        return []
    if not isinstance(nccl, dict):
        return ["distributed.nccl must be a mapping of NCCL_* variables"]
    errors = []
    for k, v in nccl.items():
        if not isinstance(k, str) or not _NCCL_KEY_RE.match(k) or _SECRETISH.search(k):
            errors.append(f"distributed.nccl key {k!r} is not an NCCL_*/TORCH_NCCL_* setting")
        elif isinstance(v, bool) or not isinstance(v, (str, int)):
            errors.append(f"distributed.nccl.{k} must be a string or integer")
        elif not _NCCL_VALUE_RE.match(str(v)):
            errors.append(f"distributed.nccl.{k} has characters outside [A-Za-z0-9_.,:/=^+-]")
    return errors


def _model_yaml(model: str, models_dir: Path | None = None) -> dict[str, Any] | None:
    """The parsed YAML of ``model`` (case-insensitive ``name:``), else ``None``."""
    if models_dir is None:
        from examlops.usecase import models_dir as _md  # noqa: PLC0415

        models_dir = _md()
    if not models_dir.is_dir():
        return None
    for path in sorted(models_dir.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            log.warning("distributed: cannot read %s", path)
            continue
        if isinstance(raw, dict) and str(raw.get("name", "")).lower() == model.lower():
            return raw
    return None


def model_declared(model: str, models_dir: Path | None = None) -> bool:
    """Whether the active use-case pack has a model YAML named ``model``."""
    return _model_yaml(model, models_dir) is not None


def yaml_block(model: str, models_dir: Path | None = None) -> dict[str, Any] | None:
    """The validated ``distributed:`` block for ``model`` (case-insensitive), else ``None``."""
    raw = _model_yaml(model, models_dir)
    block = raw.get("distributed") if raw else None
    if not block:
        return None
    errors = validate_block(block)
    if errors:
        raise ValueError(f"{model}: " + "; ".join(errors))
    return dict(block)


def resolve_plan(
    model: str,
    *,
    models_dir: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> DistributedPlan:
    """The model's plan: YAML block, then explicit ``overrides`` (flags win; ``None`` = unset)."""
    block = yaml_block(model, models_dir) or {}
    given = {k: v for k, v in (overrides or {}).items() if v is not None}
    errors = validate_block({k: v for k, v in given.items() if k in KEYS})
    if errors:
        raise ValueError("; ".join(errors))
    merged = {**block, **given}
    gpus = int(merged.get("gpus_per_node", 0))
    plan = DistributedPlan(
        model=model,
        strategy=str(merged.get("strategy", DEFAULT_STRATEGY)),
        nodes=int(merged.get("nodes", 1)),
        min_nodes=merged.get("min_nodes"),
        gpus_per_node=gpus,
        nproc_per_node=int(merged.get("nproc_per_node", gpus or 1)),
        max_restarts=int(merged.get("max_restarts", 0)),
        max_attempts=int(merged.get("max_attempts", 3)),
        steps=int(merged.get("steps", 12)),
        checkpoint_every=int(merged.get("checkpoint_every", 4)),
        entrypoint=merged.get("entrypoint"),
        nccl={str(k): str(v) for k, v in (merged.get("nccl") or {}).items()},
        source=("yaml+flags" if given else "yaml") if block else ("flags" if given else "defaults"),
    )
    if plan.min_nodes is not None and plan.min_nodes > plan.nodes:
        raise ValueError(f"min_nodes ({plan.min_nodes}) exceeds nodes ({plan.nodes})")
    return plan


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # a missing parent package raises
        return False


def preflight(plan: DistributedPlan) -> list[str]:
    """Reasons ``plan`` cannot run here (empty = go). Pure; imports nothing heavy."""
    problems: list[str] = []
    if not _importable("torch"):
        problems.append("torch is not installed (pip install torch)")
    if (
        plan.strategy == "fsdp"
        and _importable("torch")
        and not _importable("torch.distributed.fsdp")
    ):
        problems.append("this torch build has no torch.distributed.fsdp")
    pkg = _STRATEGY_PACKAGE.get(plan.strategy)
    if pkg and not _importable(pkg):
        problems.append(
            f"strategy {plan.strategy!r} needs {pkg.split('.')[0]!r}, which is not installed; "
            "FSDP (strategy: fsdp) is the dependency-free ZeRO-3-equivalent"
        )
    if plan.strategy not in REFERENCE_STRATEGIES and not plan.entrypoint:
        problems.append(
            f"strategy {plan.strategy!r} needs a model entrypoint (distributed.entrypoint): the "
            f"reference script implements {', '.join(REFERENCE_STRATEGIES)} only"
        )
    if plan.entrypoint and not _importable(plan.entrypoint):
        problems.append(f"entrypoint module {plan.entrypoint!r} is not importable")
    if plan.nccl and plan.gpus_per_node == 0:
        problems.append("NCCL settings were given for a CPU plan (gpus_per_node: 0 uses gloo)")
    return problems


def require_runnable(plan: DistributedPlan) -> None:
    problems = preflight(plan)
    if problems:
        raise StrategyUnavailable(
            f"{plan.model}: cannot run distributed plan: " + "; ".join(problems)
        )


__all__ = [
    "DEFAULT_STRATEGY",
    "KEYS",
    "REFERENCE_STRATEGIES",
    "STRATEGIES",
    "DistributedPlan",
    "StrategyUnavailable",
    "model_declared",
    "preflight",
    "require_runnable",
    "resolve_plan",
    "validate_block",
    "validate_nccl",
    "yaml_block",
]
