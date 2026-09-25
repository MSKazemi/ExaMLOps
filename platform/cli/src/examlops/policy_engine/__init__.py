"""Next-Gen 40 · D5 — policy-as-code governance (OPA/Rego) (ADR 0029).

A ``PolicyEngine`` seam that evaluates a **versioned, signed, per-tenant policy bundle** at
every key governance decision point (promotion, approvals, budget/GPU, model-card,
tenancy, supply-chain), with structured input, dry-run explainability, and audited
decisions.

This layers *on top of* the existing ``examlops.policy`` YAML engine (ADR 0079) rather than
replacing it:

- ``PolicyEngine.evaluate(decision, input) -> EngineDecision{allow, reasons}`` is the seam.
- ``YamlPolicyEngine`` (default) delegates to ``examlops.policy.decide`` — no OPA needed.
- ``RegoPolicyEngine`` uses the ``opa`` binary when available + selected
  (``EXAMLOPS_POLICY_ENGINE=opa``); it **degrades** to the YAML engine when OPA is absent.

Fail modes (R4): security-critical decisions **fail closed** (deny) on engine error; other
decisions may run in ``monitor`` mode (fail-open) before ``enforce``.

Domain gates encode built-in **default-deny** safety that a bundle can only tighten, never
loosen — e.g. an unsigned artifact is always denied deployment (R2/GWT-4).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# Security-critical decisions that MUST fail closed on engine error (R4).
FAIL_CLOSED_DECISIONS = {
    "supply_chain",
    "deploy",
    "budget",
    "tenancy",
    "slo",
    "datasheet",
    "residency",
}


@dataclass
class PolicyInput:
    """Structured policy input (R1): subject · resource · action · context."""

    action: str
    subject: str | None = None
    resource: str | None = None
    tenant: str = "default"
    context: dict[str, Any] = field(default_factory=dict)

    def flat_context(self) -> dict[str, Any]:
        """Flatten to the context shape the YAML engine's conditions evaluate over."""
        ctx = dict(self.context)
        ctx.setdefault("subject", self.subject)
        ctx.setdefault("resource", self.resource)
        ctx.setdefault("tenant", self.tenant)
        return ctx


@dataclass
class EngineDecision:
    allow: bool
    reasons: list[str]
    effect: str  # allow | deny | require_approval
    engine: str  # yaml | opa

    @property
    def requires_approval(self) -> bool:
        return self.effect == "require_approval"


class PolicyEngine(Protocol):
    def evaluate(self, decision: str, input: PolicyInput) -> EngineDecision: ...  # noqa: A002


class YamlPolicyEngine:
    """Default engine — delegates to the ADR-0079 ``examlops.policy.decide`` core."""

    name = "yaml"

    def evaluate(self, decision: str, input: PolicyInput) -> EngineDecision:  # noqa: A002
        from examlops.policy import decide

        # `decision` is the governed decision point; `input.action` its verb. We evaluate on
        # the action (matching existing policy rules) and never double-audit here — the
        # top-level `evaluate()` audits once.
        d = decide(input.action, input.flat_context(), audit=False)
        notes = [f"monitor: rule {r!r} would {e} (not enforced)" for r, e in d.shadow]
        return EngineDecision(
            allow=d.allowed,
            reasons=[d.reason, *notes],
            effect=d.effect,
            engine=self.name,
        )


class RegoPolicyEngine:
    """OPA/Rego engine — used when the ``opa`` binary is present + selected.

    Kept minimal: it shells out to ``opa eval`` against a bundle dir. When OPA is not
    available the caller falls back to :class:`YamlPolicyEngine` (graceful degradation).
    """

    name = "opa"

    def __init__(self, bundle_dir: str) -> None:
        self.bundle_dir = bundle_dir

    @staticmethod
    def available() -> bool:
        import shutil

        return shutil.which("opa") is not None

    def evaluate(self, decision: str, input: PolicyInput) -> EngineDecision:  # noqa: A002
        import json
        import subprocess

        query = f"data.examlops.{decision}.allow"
        try:
            proc = subprocess.run(
                ["opa", "eval", "-b", self.bundle_dir, "-I", "-f", "json", query],
                input=json.dumps(input.flat_context()),
                capture_output=True,
                text=True,
                timeout=10,
            )
            out = json.loads(proc.stdout or "{}")
            value = out["result"][0]["expressions"][0]["value"]
            allow = bool(value)
            return EngineDecision(
                allow=allow,
                reasons=["opa: allow" if allow else "opa: deny"],
                effect="allow" if allow else "deny",
                engine=self.name,
            )
        except Exception as exc:  # let the top-level decide fail-open/closed
            raise RuntimeError(f"opa evaluation failed: {exc}") from exc


class ProviderEngine:
    """Adapts a ``policy``-domain :class:`Provider` (an ``exa.providers.policy`` plugin) to the
    :class:`PolicyEngine` seam."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.name = str(getattr(provider, "name", "plugin"))

    def evaluate(self, decision: str, input: PolicyInput) -> EngineDecision:  # noqa: A002
        out = self.provider.compute(
            {
                "decision": decision,
                "action": input.action,
                "subject": input.subject,
                "resource": input.resource,
                "tenant": input.tenant,
                "context": input.flat_context(),
            }
        )
        effect = str(out.get("effect") or ("allow" if out.get("allow") else "deny")).lower()
        if effect not in ("allow", "deny", "require_approval"):
            effect = "deny"  # an engine speaking an unknown effect is not granting anything
        return EngineDecision(
            allow=effect == "allow",
            reasons=[str(r) for r in (out.get("reasons") or [f"{self.name}: {effect}"])],
            effect=effect,
            engine=self.name,
        )


def get_engine() -> PolicyEngine:
    """Select the policy engine: the YAML core (default), OPA, or an ``exa.providers.policy`` plugin.

    ``EXAMLOPS_POLICY_ENGINE`` names it. OPA requested but the binary absent falls back to YAML
    (unchanged). A plugin that cannot be resolved/loaded falls back to YAML **audibly**
    (``degraded_to_default``) — a configured engine that is broken must not look like none.
    """
    raw = os.getenv("EXAMLOPS_POLICY_ENGINE", "").strip()
    name = raw.lower()
    if name in ("", "yaml"):
        return YamlPolicyEngine()
    if name == "opa":
        if RegoPolicyEngine.available():
            bundle = os.getenv(
                "EXAMLOPS_POLICY_BUNDLE_DIR", str(Path.home() / ".config" / "examlops" / "bundle")
            )
            return RegoPolicyEngine(bundle)
        return YamlPolicyEngine()
    try:
        from examlops.providers import get_provider

        from . import providers as _providers  # noqa: F401 - registers the built-ins

        return ProviderEngine(get_provider(_providers.DOMAIN, name=raw))
    except Exception as exc:  # noqa: BLE001 - an unloadable plugin degrades, never crashes
        from examlops.providers.loader import degraded_to_default

        degraded_to_default("policy", exc, configured=raw)
        return YamlPolicyEngine()


def evaluate(
    decision: str,
    input: PolicyInput,  # noqa: A002
    *,
    fail_closed: bool | None = None,
    audit: bool = True,
) -> EngineDecision:
    """Evaluate a governed decision through the engine, then audit it (R3/R7).

    ``fail_closed`` defaults from :data:`FAIL_CLOSED_DECISIONS`. On engine error a
    fail-closed decision denies; otherwise it allows in ``monitor`` spirit (R4).
    """
    if fail_closed is None:
        fail_closed = decision in FAIL_CLOSED_DECISIONS
    engine = get_engine()
    try:
        result = engine.evaluate(decision, input)
    except Exception as exc:
        if fail_closed:
            result = EngineDecision(
                allow=False,
                reasons=[f"engine error (fail-closed): {exc}"],
                effect="deny",
                engine=getattr(engine, "name", "unknown"),
            )
        else:
            result = EngineDecision(
                allow=True,
                reasons=[f"engine error (fail-open/monitor): {exc}"],
                effect="allow",
                engine=getattr(engine, "name", "unknown"),
            )
    if audit:
        _audit(decision, input, result)
    return result


# --- Domain gates: built-in default-deny safety a bundle can only tighten -------------
def supply_chain_gate(
    model: str, version: str, *, signed: bool, tenant: str = "default"
) -> EngineDecision:
    """Deny deployment of an **unsigned** artifact (R2/GWT-4). Signed → consult the bundle."""
    if not signed:
        result = EngineDecision(
            allow=False,
            reasons=[f"artifact {model}/{version} is unsigned — deployment denied (D3)"],
            effect="deny",
            engine="builtin",
        )
        _audit(
            "supply_chain",
            PolicyInput("deploy", resource=f"{model}/{version}", tenant=tenant),
            result,
        )
        return result
    return evaluate(
        "supply_chain",
        PolicyInput(
            "deploy", resource=f"{model}/{version}", tenant=tenant, context={"signed": True}
        ),
    )


def budget_gate(
    gpu_hours_requested: float, budget_gpu_hours: float, *, tenant: str = "default"
) -> EngineDecision:
    """Deny a GPU request over budget (R2/GWT-3)."""
    over = gpu_hours_requested > budget_gpu_hours
    if over:
        result = EngineDecision(
            allow=False,
            reasons=[
                f"requested {gpu_hours_requested} GPU-h > budget {budget_gpu_hours} GPU-h — denied"
            ],
            effect="deny",
            engine="builtin",
        )
        _audit("budget", PolicyInput("allocate", tenant=tenant), result)
        return result
    return evaluate(
        "budget",
        PolicyInput(
            "allocate",
            tenant=tenant,
            context={"gpu_hours": gpu_hours_requested, "budget": budget_gpu_hours},
        ),
    )


def card_gate(
    model: str, completeness: float, *, floor: float = 0.8, tenant: str = "default"
) -> EngineDecision:
    """Require model-card completeness ≥ floor before promotion (R2/A6)."""
    if completeness < floor:
        result = EngineDecision(
            allow=False,
            reasons=[
                f"model-card completeness {completeness:.2f} < floor {floor:.2f} — promotion denied"
            ],
            effect="deny",
            engine="builtin",
        )
        _audit("model_card", PolicyInput("promote", resource=model, tenant=tenant), result)
        return result
    return evaluate(
        "model_card",
        PolicyInput(
            "promote", resource=model, tenant=tenant, context={"completeness": completeness}
        ),
    )


def slo_gate(servable: str, reasons: list[str], *, tenant: str = "default") -> EngineDecision:
    """Refuse promotion unless every declared SLOSpec is met (ADR 0148 d3).

    ``reasons`` are the unmet conditions computed by :mod:`examlops.slo.specs` (no spec declared,
    a violated objective, no verdict). Empty ``reasons`` defers to the YAML engine, so a site rule
    on the ``slo_gate`` action can still tighten - never loosen - the built-in refusal.
    """
    if reasons:
        result = EngineDecision(
            allow=False,
            reasons=[f"SLO gate: {r}" for r in reasons],
            effect="deny",
            engine="builtin",
        )
        _audit("slo", PolicyInput("promote", resource=servable, tenant=tenant), result)
        return result
    return evaluate("slo", PolicyInput("slo_gate", resource=servable, tenant=tenant))


def datasheet_gate(model: str, reasons: list[str], *, tenant: str = "default") -> EngineDecision:
    """Refuse promotion unless the model's training datasets carry a datasheet (ADR 0079 d6).

    ``reasons`` come from :func:`examlops.cards.datasheet.promotion_reasons` (no datasheet, or
    one below the floor). Empty ``reasons`` defers to the engine, so a site rule on the
    ``datasheet`` action can still tighten — never loosen — the built-in refusal.
    """
    if reasons:
        result = EngineDecision(
            allow=False,
            reasons=[f"datasheet gate: {r}" for r in reasons],
            effect="deny",
            engine="builtin",
        )
        _audit("datasheet", PolicyInput("promote", resource=model, tenant=tenant), result)
        return result
    return evaluate("datasheet", PolicyInput("promote", resource=model, tenant=tenant))


def residency_gate(
    model: str, cluster: str, reasons: list[str], *, tenant: str = "default"
) -> EngineDecision:
    """Refuse to train where a dataset may not be processed (ADR 0029 d2, D6 residency).

    ``reasons`` come from :func:`examlops.policy_engine.residency.residency_reasons`. Empty
    defers to the engine (a site rule on ``residency`` may tighten, never loosen).
    """
    resource = f"{model}@{cluster}"
    if reasons:
        result = EngineDecision(
            allow=False,
            reasons=[f"residency gate: {r}" for r in reasons],
            effect="deny",
            engine="builtin",
        )
        _audit("residency", PolicyInput("allocate", resource=resource, tenant=tenant), result)
        return result
    return evaluate(
        "residency",
        PolicyInput("allocate", resource=resource, tenant=tenant, context={"cluster": cluster}),
    )


# --- Signed, versioned bundle (R2, D3 signing) ---------------------------------------
def _bundle_content(tenant: str = "default") -> str:
    """The effective policy text for a tenant: base policy.yaml + optional tenant overlay (R7)."""
    from examlops.policy import POLICY_YAML

    parts: list[str] = []
    base = Path(POLICY_YAML)
    if base.is_file():
        parts.append(base.read_text())
    overlay = base.with_name(f"policy.{tenant}.yaml")
    if tenant != "default" and overlay.is_file():
        parts.append(f"# --- tenant overlay: {tenant} ---\n{overlay.read_text()}")
    return "\n".join(parts) if parts else "# (no policies — default allow)\n"


def sign_bundle(tenant: str = "default", *, actor: str | None = None) -> dict[str, Any]:
    """Version + hash + sign the effective policy bundle for a tenant (R2)."""
    from examlops import data as platform_db

    content = _bundle_content(tenant)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    signature, algo = _sign(content_hash)
    version = platform_db.store_policy_bundle(
        tenant, content, content_hash, signature=signature, algo=algo, signed_by=actor
    )
    _audit_bundle(
        tenant,
        version,
        "policy_bundle_signed",
        {"hash": content_hash[:16], "signed": signature is not None},
        actor,
    )
    return {
        "tenant": tenant,
        "version": version,
        "content_hash": content_hash,
        "signed": signature is not None,
    }


def verify_bundle(tenant: str = "default", version: int | None = None) -> dict[str, Any]:
    """Verify a stored bundle's hash + signature (R2)."""
    from examlops import data as platform_db

    row = platform_db.get_policy_bundle(tenant, version)
    if not row:
        return {"tenant": tenant, "present": False, "valid": False, "reasons": ["no bundle"]}
    reasons: list[str] = []
    recomputed = hashlib.sha256(row["content"].encode()).hexdigest()
    if recomputed != row["content_hash"]:
        reasons.append("content hash mismatch (tampered)")
    if row.get("signature"):
        expected, _ = _sign(row["content_hash"])
        if expected is not None and expected != row["signature"]:
            reasons.append("signature mismatch")
    else:
        reasons.append("unsigned bundle")
    return {
        "tenant": tenant,
        "version": row["version"],
        "present": True,
        "valid": not reasons,
        "reasons": reasons or ["ok"],
    }


def _sign(content_hash: str) -> tuple[str | None, str | None]:
    try:
        from examlops.supplychain import _hmac_sign

        return _hmac_sign(content_hash), "hmac-sha256"
    except Exception:
        return None, None


def _audit(decision: str, input: PolicyInput, result: EngineDecision) -> None:  # noqa: A002
    # A policy decision that happened and was not recorded is the gap an auditor is looking for,
    # so the loss is counted rather than swallowed. The decision itself still stands: the engine
    # never refuses because the audit store is unavailable.
    from examlops.data.audit import audit_best_effort

    audit_best_effort(
        "policy-engine",
        input.subject,
        f"policy_{decision}",
        input.resource or input.action,
        {"effect": result.effect, "reasons": result.reasons, "engine": result.engine},
        tenant=input.tenant,
    )


def _audit_bundle(
    tenant: str, version: int, action: str, extra: dict[str, Any], actor: str | None
) -> None:
    from examlops.data.audit import audit_best_effort

    audit_best_effort("policy-engine", actor, action, f"{tenant}/v{version}", extra, tenant=tenant)


__all__ = [
    "PolicyInput",
    "EngineDecision",
    "PolicyEngine",
    "YamlPolicyEngine",
    "RegoPolicyEngine",
    "FAIL_CLOSED_DECISIONS",
    "ProviderEngine",
    "get_engine",
    "evaluate",
    "supply_chain_gate",
    "budget_gate",
    "card_gate",
    "slo_gate",
    "datasheet_gate",
    "residency_gate",
    "sign_bundle",
    "verify_bundle",
]
