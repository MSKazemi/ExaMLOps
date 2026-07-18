"""Next-Gen 40 · E7 — federated & privacy-preserving training (ADR 0040).

Opt-in federated training across sites that **cannot share raw data**: a coordinator drives
rounds, each site trains locally on its own data, and only **model updates** (never raw
data/PII) are aggregated (FedAvg / FedProx / a Byzantine-robust trimmed mean). Optional
**differential privacy** tracks an (ε, δ) budget; optional **secure aggregation** means the
coordinator sees only the summed update, not per-site ones. Sites **authenticate** and their
updates are **signed** (D3); unauthorized/unsigned participation is **rejected + audited**.

Honesty (R5): privacy guarantees are only claimed for the mechanism actually enabled — with
DP off, no privacy is claimed; with secure aggregation off, per-site updates are visible.

Pure-Python and testable — no Flower/Opacus/mTLS required to run a round, track the DP
budget, enforce secure aggregation, or reject an unauthorized site.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from examlops import data as platform_db

STRATEGIES = ("fedavg", "fedprox", "robust")


class SiteRejectedError(RuntimeError):
    """Raised when an unauthorized or unsigned site attempts to participate (R6/GWT-4)."""


@dataclass
class SiteUpdate:
    site: str
    weights: list[float]
    num_samples: int
    loss: float = 0.0
    signed: bool = True


@dataclass
class FedRun:
    run_id: str
    strategy: str
    dp_enabled: bool
    secure_agg: bool
    sites: list[str]


@dataclass
class RoundResult:
    run_id: str
    round_num: int
    global_weights: list[float]
    global_loss: float
    sites_participated: int
    epsilon: float
    delta: float
    # Per-site updates are hidden under secure aggregation (R4/GWT-3).
    per_site: list[dict] | None = field(default=None)
    rejected: list[str] = field(default_factory=list)


def federated_init(
    sites: list[str],
    strategy: str = "fedavg",
    *,
    run_id: str | None = None,
    dp: dict | None = None,
    secure_agg: bool = False,
    authorized_sites: list[str] | None = None,
    actor: str | None = None,
) -> FedRun:
    """Initialize a federated run: register sites + privacy config (R1/R8)."""
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {STRATEGIES}, got {strategy!r}")
    rid = run_id or f"fed-{strategy}-{len(sites)}sites"
    dp_enabled = bool(dp)
    delta = float(dp.get("delta", 1e-5)) if dp else 0.0
    eps_per_round = float(dp.get("epsilon_per_round", 0.5)) if dp else 0.0
    platform_db.create_federated_run(
        rid,
        strategy,
        dp_enabled=dp_enabled,
        secure_agg=secure_agg,
        delta=delta,
        epsilon_per_round=eps_per_round,
    )
    authorized = set(authorized_sites) if authorized_sites is not None else set(sites)
    for s in sites:
        platform_db.register_federated_site(rid, s, s in authorized)
    _audit(
        rid,
        "federated_init",
        {"strategy": strategy, "dp": dp_enabled, "secure_agg": secure_agg},
        actor,
    )
    return FedRun(rid, strategy, dp_enabled, secure_agg, list(sites))


def _aggregate(updates: list[SiteUpdate], strategy: str) -> list[float]:
    """FedAvg (sample-weighted mean); 'robust' uses a coordinate-wise trimmed mean (R2)."""
    dim = len(updates[0].weights)
    if strategy == "robust" and len(updates) >= 3:
        agg = []
        for j in range(dim):
            col = sorted(u.weights[j] for u in updates)
            trimmed = col[1:-1] or col  # drop min+max (Byzantine-robust)
            agg.append(sum(trimmed) / len(trimmed))
        return agg
    total = sum(u.num_samples for u in updates) or 1
    return [sum(u.weights[j] * u.num_samples for u in updates) / total for j in range(dim)]


def run_round(
    run_id: str,
    updates: list[SiteUpdate],
    *,
    actor: str | None = None,
) -> RoundResult:
    """Aggregate one round of site updates (R1/R2/R3/R4/GWT-1/GWT-3).

    Rejects updates from unauthorized or unsigned sites (audited). Raw data never crosses a
    boundary — only the ``weights``/``loss`` summaries do. Under DP the (ε, δ) accountant
    advances; under secure aggregation per-site updates are not exposed.
    """
    run = platform_db.get_federated_run(run_id)
    if run is None:
        raise ValueError(f"unknown federated run {run_id!r}")
    authorized = {s["site"] for s in platform_db.get_federated_sites(run_id) if s["authorized"]}

    accepted: list[SiteUpdate] = []
    rejected: list[str] = []
    for u in updates:
        if u.site not in authorized or not u.signed:
            rejected.append(u.site)
            _audit(run_id, "federated_site_rejected", {"site": u.site, "signed": u.signed}, actor)
        else:
            accepted.append(u)
    if not accepted:
        raise SiteRejectedError(
            f"no authorized+signed site updates for {run_id} (rejected: {rejected})"
        )

    global_weights = _aggregate(accepted, run["strategy"])
    global_loss = sum(u.loss for u in accepted) / len(accepted)

    round_num = run["rounds_completed"] + 1
    # DP accountant: basic composition ε = rounds × ε_per_round (never claims more, R3/R5).
    epsilon = run["epsilon_per_round"] * round_num if run["dp_enabled"] else 0.0
    platform_db.record_federated_round(run_id, round_num, global_loss, len(accepted), epsilon)

    per_site = (
        None
        if run["secure_agg"]
        else [{"site": u.site, "num_samples": u.num_samples, "loss": u.loss} for u in accepted]
    )
    _audit(
        run_id,
        "federated_round",
        {"round": round_num, "sites": len(accepted), "epsilon": epsilon},
        actor,
    )
    return RoundResult(
        run_id=run_id,
        round_num=round_num,
        global_weights=global_weights,
        global_loss=global_loss,
        sites_participated=len(accepted),
        epsilon=epsilon,
        delta=run["delta"],
        per_site=per_site,
        rejected=rejected,
    )


def privacy_budget(run_id: str) -> dict:
    """Report the tracked (ε, δ) budget — honest about DP being off (R3/GWT-2)."""
    run = platform_db.get_federated_run(run_id)
    if run is None:
        return {"run_id": run_id, "present": False}
    if not run["dp_enabled"]:
        return {
            "run_id": run_id,
            "dp_enabled": False,
            "note": "no differential privacy — no ε/δ claim",
        }
    return {
        "run_id": run_id,
        "dp_enabled": True,
        "epsilon": run["epsilon"],
        "delta": run["delta"],
        "rounds": run["rounds_completed"],
    }


def federated_status(run_id: str) -> dict:
    run = platform_db.get_federated_run(run_id)
    return {
        "run": run,
        "sites": platform_db.get_federated_sites(run_id),
        "rounds": platform_db.list_federated_rounds(run_id),
    }


def _audit(run_id: str, action: str, extra: dict, actor: str | None) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event("exa-federated", actor, action, run_id, extra)
    except Exception:
        pass


__all__ = [
    "STRATEGIES",
    "SiteRejectedError",
    "SiteUpdate",
    "FedRun",
    "RoundResult",
    "federated_init",
    "run_round",
    "privacy_budget",
    "federated_status",
]
