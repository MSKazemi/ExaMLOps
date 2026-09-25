"""LoRA adapters are supply chain (ADR 0143 decision 8).

Two checks, both run by every launcher before a ``vllm serve`` process is started
(:func:`preflight`):

1. **No runtime adapter updating on a shared deployment.** vLLM's own documentation calls runtime
   LoRA loading unsafe outside a fully trusted environment; it is switched on by the
   ``VLLM_ALLOW_RUNTIME_LORA_UPDATING`` environment variable, which a launcher would otherwise pass
   through from the operator's shell. The config flag is refused by
   :func:`examlops.engines.config.lora_args`; the environment variable is refused here.
2. **Verify-before-load.** Every static adapter must name a registered, signed artifact
   (``model`` + ``version``) and verify against its bytes through
   :func:`examlops.supplychain.verify_before_load` — the same gate a model load uses, so an armed
   ``supply_chain`` policy gate governs adapters too. An adapter with no registry identity is
   refused in ``enforce`` mode (the default): an unregistered adapter cannot have been signed.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from examlops.engines.config import EngineConfig, LoraAdapter, RuntimeLoraRefused

__all__ = [
    "RUNTIME_LORA_ENV",
    "AdapterVerification",
    "LoraPreflightError",
    "preflight",
    "runtime_lora_env_violation",
    "verify_lora_adapters",
]

RUNTIME_LORA_ENV = "VLLM_ALLOW_RUNTIME_LORA_UPDATING"
_TRUTHY = {"1", "true", "yes", "on"}

Verifier = Callable[[str, str, list[Path]], bool]


class LoraPreflightError(RuntimeError):
    """A launch was refused by the LoRA supply-chain preflight."""


@dataclass
class AdapterVerification:
    ok: bool
    refused: list[dict[str, str]] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)


def runtime_lora_env_violation(env: Mapping[str, str], config: EngineConfig) -> str | None:
    """A reason string when ``env`` would enable runtime LoRA updating on a shared deployment."""
    raw = str(env.get(RUNTIME_LORA_ENV, "")).strip().lower()
    if raw in _TRUTHY and config.shared:
        return (
            f"{RUNTIME_LORA_ENV}={env.get(RUNTIME_LORA_ENV)} is refused on a shared deployment "
            "(ADR 0143 d8); unset it, or declare the endpoint single-tenant with shared: false"
        )
    return None


def _bundle(path: Path) -> tuple[list[Path], Path]:
    """The adapter's files and bundle root, exactly as ``exa models sign --path <p>`` collects them.

    A LoRA adapter is a directory (``adapter_config.json`` + weights). The signature covers the
    files *inside* it, relative to it, so handing the directory itself to the digest would hash
    nothing — and "nothing" must never be what a signature is checked against.
    """
    if path.is_file():
        return [path], path.parent
    files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else []
    if not files:
        raise FileNotFoundError(f"no adapter files at {path} on this host; nothing to verify")
    return files, path


def _default_verifier(mode: str) -> Verifier:
    def verify(model: str, version: str, paths: list[Path]) -> bool:
        from examlops.supplychain import verify_before_load

        files: list[Path] = []
        root: Path | None = None
        for p in paths:
            found, root = _bundle(Path(p))
            files.extend(found)
        return verify_before_load(model, version, files, mode=mode, root=root)

    return verify


def verify_lora_adapters(
    config: EngineConfig, *, mode: str = "enforce", verifier: Verifier | None = None
) -> AdapterVerification:
    """Verify every static adapter before load. ``warn`` records but does not refuse."""
    check = verifier or _default_verifier(mode)
    result = AdapterVerification(ok=True)
    for adapter in config.lora_adapters:
        reason = _verify_one(adapter, check)
        if reason is None:
            result.verified.append(adapter.name)
        else:
            result.refused.append({"adapter": adapter.name, "reason": reason})
    if result.refused and mode == "enforce":
        result.ok = False
    return result


def _verify_one(adapter: LoraAdapter, check: Verifier) -> str | None:
    if not adapter.model or not adapter.version:
        return "unregistered: no registry model/version, so no signature can exist"
    try:
        ok = check(adapter.model, adapter.version, [Path(adapter.path)])
    except Exception as exc:  # noqa: BLE001 - a verification that cannot run is a failure
        return f"verification could not run: {exc}"
    return None if ok else "signature verification failed"


def preflight(
    config: EngineConfig,
    *,
    env: Mapping[str, str] | None = None,
    mode: str | None = None,
    verifier: Verifier | None = None,
) -> AdapterVerification:
    """Run both decision-8 checks; raise :class:`LoraPreflightError` on refusal.

    ``mode`` defaults to ``EXAMLOPS_LORA_VERIFY_MODE`` (``enforce``). A config without adapters
    and without runtime updating passes without touching the registry.
    """
    environ = os.environ if env is None else env
    if config.allow_runtime_lora_updating and config.shared:
        raise LoraPreflightError(
            str(RuntimeLoraRefused("runtime LoRA updating is refused on a shared deployment"))
        )
    violation = runtime_lora_env_violation(environ, config)
    if violation:
        raise LoraPreflightError(violation)
    if not config.lora_adapters:
        return AdapterVerification(ok=True)
    chosen = (mode or os.getenv("EXAMLOPS_LORA_VERIFY_MODE") or "enforce").strip().lower()
    if chosen not in ("enforce", "warn"):
        chosen = "enforce"  # fail closed on a typo
    result = verify_lora_adapters(config, mode=chosen, verifier=verifier)
    if not result.ok:
        detail = "; ".join(f"{r['adapter']}: {r['reason']}" for r in result.refused)
        raise LoraPreflightError(f"LoRA adapter verification refused the launch: {detail}")
    return result
