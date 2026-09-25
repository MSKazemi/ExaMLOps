"""ADR 0030 decision 2 — a KServe pod requests a *fraction* of a GPU.

A model YAML may declare, at top level::

    gpu_sharing:
      fraction: 0.25          # (0, 1]; defaults to autoscale.gpu_fraction when omitted
      mig_profile: 1g.5gb     # optional; implies mechanism: mig
      mechanism: mig          # mig | timeslice | hami   (default: mig if mig_profile else timeslice)
      timeslice_resource: nvidia.com/gpu   # device-plugin resource name for time-slicing

and the KServe renderer (:mod:`examlops.serving.substrates.kserve`) puts the matching
extended-resource **limits** on the model-server container. The mechanisms are the open ones the
ADR names; each is a request the cluster's own device plugin/scheduler honours:

* **mig** — NVIDIA GPU Operator, *mixed* MIG strategy: ``nvidia.com/mig-<profile>: 1``. Hardware
  isolation. With no profile the fraction is snapped **up** to the smallest profile that holds it
  (A100-40GB 7-slice table, :data:`examlops.gpu_sharing.MIG_FRACTIONS`) and the waste is recorded.
* **timeslice** — NVIDIA device-plugin time-slicing: the pod asks for one (shared) device; the
  fraction is **not enforced** (soft isolation) and is recorded in annotations so nobody reads it
  as a guarantee. ``timeslice_resource`` names the resource when the plugin renames shared GPUs
  (``renameByDefault: true`` → ``nvidia.com/gpu.shared``).
* **hami** — HAMi (CNCF sandbox) fractional scheduling: ``nvidia.com/gpu: 1`` plus
  ``nvidia.com/gpumem-percentage`` and ``nvidia.com/gpucores`` at the fraction (enforced in-container
  by HAMi-core).

DRA/MPS claims are not rendered (see the ADR status). A model YAML with no ``gpu_sharing:`` block
and no sub-1.0 ``autoscale.gpu_fraction`` renders exactly as before — no GPU request at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from examlops.gpu_sharing import MIG_FRACTIONS

MECHANISMS = ("mig", "timeslice", "hami")
KEYS: dict[str, type | tuple[type, ...]] = {
    "fraction": (int, float),
    "mig_profile": str,
    "mechanism": str,
    "timeslice_resource": str,
}
_ISOLATION = {
    "mig": "hardware",
    "timeslice": "soft",
    "hami": "soft-enforced",
    "whole": "exclusive",
}

GPU_ANNOTATION_PREFIX = "examlops.io/gpu-"


@dataclass(frozen=True)
class K8sGpuRequest:
    mechanism: str
    requested_fraction: float
    allocated_fraction: float
    limits: dict[str, str] = field(default_factory=dict)
    mig_profile: str | None = None

    @property
    def isolation(self) -> str:
        return _ISOLATION[self.mechanism]

    def annotations(self) -> dict[str, str]:
        out = {
            f"{GPU_ANNOTATION_PREFIX}mechanism": self.mechanism,
            f"{GPU_ANNOTATION_PREFIX}isolation": self.isolation,
            f"{GPU_ANNOTATION_PREFIX}fraction": f"{self.requested_fraction:g}",
            f"{GPU_ANNOTATION_PREFIX}allocated-fraction": f"{self.allocated_fraction:.4f}",
        }
        if self.mig_profile:
            out[f"{GPU_ANNOTATION_PREFIX}mig-profile"] = self.mig_profile
        return out

    def resources(self) -> dict[str, dict[str, str]]:
        # Extended resources: requests default to limits and may not differ from them.
        return {"limits": dict(self.limits)}


def validate_gpu_sharing_block(block: Any, *, autoscale: Any = None) -> list[str]:
    """Errors in a model YAML ``gpu_sharing:`` block (empty = valid). Structural, no I/O."""
    if block in (None, {}):
        return []
    if not isinstance(block, dict):
        return ["gpu_sharing must be a mapping"]
    errors = [f"unknown gpu_sharing key {k!r}" for k in block if k not in KEYS]
    for key, want in KEYS.items():
        if key in block and (isinstance(block[key], bool) or not isinstance(block[key], want)):
            errors.append(f"gpu_sharing.{key} has the wrong type ({type(block[key]).__name__})")
    if errors:
        return errors
    if "fraction" in block and not (0.0 < float(block["fraction"]) <= 1.0):
        errors.append("gpu_sharing.fraction must be in (0, 1]")
    mig = block.get("mig_profile")
    if mig is not None and mig not in MIG_FRACTIONS:
        errors.append(
            f"gpu_sharing.mig_profile {mig!r} unknown (known: {', '.join(MIG_FRACTIONS)})"
        )
    mech = block.get("mechanism")
    if mech is not None and mech not in MECHANISMS:
        errors.append(f"gpu_sharing.mechanism {mech!r} not one of {MECHANISMS}")
    if mig is not None and mech not in (None, "mig"):
        errors.append("gpu_sharing.mig_profile only applies to mechanism 'mig'")
    if "timeslice_resource" in block and not str(block["timeslice_resource"]).strip():
        errors.append("gpu_sharing.timeslice_resource must not be empty")
    auto_frac = autoscale.get("gpu_fraction") if isinstance(autoscale, dict) else None
    if (
        "fraction" in block
        and isinstance(auto_frac, (int, float))
        and not isinstance(auto_frac, bool)
        and abs(float(auto_frac) - float(block["fraction"])) > 1e-9
    ):
        errors.append(
            f"gpu_sharing.fraction {block['fraction']} disagrees with autoscale.gpu_fraction "
            f"{auto_frac} — one replica cannot have two GPU shares"
        )
    return errors


def _snap_mig(fraction: float) -> tuple[str, float]:
    for profile, frac in sorted(MIG_FRACTIONS.items(), key=lambda kv: kv[1]):
        if frac + 1e-9 >= fraction:
            return profile, frac
    return "7g.40gb", 1.0  # pragma: no cover - MIG_FRACTIONS ends at 1.0


def k8s_gpu_request(model_yaml: dict[str, Any]) -> K8sGpuRequest | None:
    """The GPU request a KServe pod should carry for ``model_yaml``, or ``None`` for none.

    Raises :class:`ValueError` on an invalid block — a manifest must never be rendered with a GPU
    shape other than the one declared.
    """
    block = model_yaml.get("gpu_sharing") or {}
    autoscale = model_yaml.get("autoscale") or {}
    errors = validate_gpu_sharing_block(block, autoscale=autoscale)
    if errors:
        raise ValueError("; ".join(errors))
    fraction = block.get("fraction")
    if fraction is None and isinstance(autoscale, dict):
        fraction = autoscale.get("gpu_fraction")
    mig = block.get("mig_profile")
    if not block and (fraction is None or float(fraction) >= 1.0):
        return None
    frac = float(fraction) if fraction is not None else (MIG_FRACTIONS[mig] if mig else 1.0)
    mechanism = block.get("mechanism") or ("mig" if mig else "timeslice")
    if frac >= 1.0 and mechanism != "mig":
        # A whole device: nothing to share, and calling it "timeslice" would misdescribe it.
        return K8sGpuRequest("whole", frac, 1.0, {"nvidia.com/gpu": "1"})

    if mechanism == "mig":
        if mig is None:
            mig, alloc = _snap_mig(frac)
        else:
            alloc = MIG_FRACTIONS[mig]
            if fraction is not None and alloc + 1e-9 < frac:
                raise ValueError(
                    f"gpu_sharing.mig_profile {mig} ({alloc:.3f} GPU) is smaller than "
                    f"fraction {frac}"
                )
        return K8sGpuRequest("mig", frac, alloc, {f"nvidia.com/mig-{mig}": "1"}, mig)
    if mechanism == "hami":
        pct = str(max(1, min(100, math.ceil(frac * 100 - 1e-9))))
        return K8sGpuRequest(
            "hami",
            frac,
            int(pct) / 100.0,
            {
                "nvidia.com/gpu": "1",
                "nvidia.com/gpumem-percentage": pct,
                "nvidia.com/gpucores": pct,
            },
        )
    resource = str(block.get("timeslice_resource") or "nvidia.com/gpu").strip()
    # Time-slicing hands the pod a whole (shared) device and enforces no share: the allocated
    # fraction is recorded as the request, flagged soft by the isolation annotation.
    return K8sGpuRequest("timeslice", frac, frac, {resource: "1"})


__all__ = [
    "GPU_ANNOTATION_PREFIX",
    "K8sGpuRequest",
    "MECHANISMS",
    "k8s_gpu_request",
    "validate_gpu_sharing_block",
]
