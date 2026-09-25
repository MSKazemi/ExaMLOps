"""Data-residency policy (ADR 0029 decision 2, D6): training data stays where it may be processed.

Two declarations, both operator-authored files and both optional:

* a dataset's **datasheet** (``examlops.cards.datasheet``) may list the regions its data may be
  processed in — ``distribution.residency: [eu, it]`` (a string or a list; case-insensitive);
* a cluster's entry in ``clusters.yaml`` (``examlops.hpc_registry``) may state its ``region:``.

:func:`residency_reasons` is the rule: a dataset with **no** residency declaration is
unconstrained; a dataset **with** one may only run on a cluster whose region is declared and in
the list — an undeclared cluster region cannot prove compliance, so it is refused (fail closed).
The ``residency`` gate (``policy.yaml`` ``gates:`` / ``EXAMLOPS_POLICY_GATES``, off by default)
applies it at ``exa pipeline run --cluster <name|auto>``; with ``auto`` (enforce) non-compliant clusters
are removed from placement's candidates first, so placement never picks a forbidden region.

The platform names no region and no dataset: both come from the site's own files.
"""

from __future__ import annotations

from typing import Any


def _norm(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list | tuple | set) else [value]
    return sorted({str(v).strip().lower() for v in items if str(v).strip()})


def allowed_regions(dataset: str) -> list[str] | None:
    """The regions ``dataset`` may be processed in, or ``None`` when it declares no constraint.

    A datasheet that exists but cannot be read is treated as a constraint nobody can satisfy
    (``[]``): residency is a security decision, and an unreadable declaration must not lift it.
    """
    from examlops.cards.datasheet import DatasheetError, load_datasheet

    try:
        doc = load_datasheet(dataset)
    except DatasheetError:
        return []
    if doc is None:
        return None
    dist = doc.get("distribution")
    raw = dist.get("residency") if isinstance(dist, dict) else None
    if raw is None:
        return None
    return _norm(raw)


def cluster_region(cluster: str) -> str | None:
    """The ``region:`` a cluster declares in ``clusters.yaml``, lower-cased, or ``None``."""
    from examlops.hpc_registry import _load_yaml

    entry = _load_yaml().get(cluster) or {}
    region = entry.get("region") if isinstance(entry, dict) else None
    if region is None:
        return None
    return str(region).strip().lower() or None


def residency_reasons(datasets: list[str], cluster: str) -> list[str]:
    """Why running on ``cluster`` would breach a dataset's residency — empty means it would not."""
    reasons: list[str] = []
    region = None
    region_read = False
    for ds in datasets:
        allowed = allowed_regions(ds)
        if allowed is None:
            continue
        if not region_read:
            region, region_read = cluster_region(cluster), True
        if not allowed:
            reasons.append(
                f"{ds}: residency declaration is empty or unreadable — no region allowed"
            )
        elif region is None:
            reasons.append(
                f"{ds} may only be processed in {allowed}; cluster '{cluster}' declares no region"
            )
        elif region not in allowed:
            reasons.append(
                f"{ds} may only be processed in {allowed}; cluster '{cluster}' is in '{region}'"
            )
    return reasons


def compliant_clusters(datasets: list[str], clusters: list[dict]) -> list[dict]:
    """``clusters`` (placement candidates, each with a ``name``) that breach no residency."""
    return [c for c in clusters if not residency_reasons(datasets, str(c.get("name")))]


__all__ = ["allowed_regions", "cluster_region", "compliant_clusters", "residency_reasons"]
