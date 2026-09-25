"""The Model Catalog (ADR 0158) — curated, provenance-tracked *deployable model definitions*.

The catalog answers **"what could I start from?"**. The MLflow registry answers **"what did we
produce, and what's live?"**. They are not two views of one thing, and conflating them is the
single most likely way to get this subsystem wrong, so the distinction is restated here:

======================  ===============================================  ============================
                        Model Catalog                                    MLflow registry
======================  ===============================================  ============================
Answers                 "what could I start from?"                       "what did we produce?"
Populated by            a curated publish, behind a trust gate           a training run
Identity                ``name@catalog_version`` (content hash)          ``name/version`` + aliases
Mutable after publish   never — a correction is a new version            yes — an alias moves
Consumed by             ``exa catalog pull`` → a NEW per-model YAML      ``exa serve``, promotion
A pull creates          a project-scoped model definition, untrained     n/a — there is no "pull"
======================  ===============================================  ============================

A catalog entry is never served, never gains an alias, and is never the target of
``exa pipeline promote`` or ``exa serve traffic``. It becomes eligible for those only once the
pulled-and-materialized model has been trained through the ordinary pipeline path, at which point
it is an ordinary registry model like any other.

Four things an entry references rather than duplicates — there is no second mechanism for any of
them: signing is :mod:`examlops.supplychain`, lineage is :mod:`examlops.lineage`, eval results are
``eval_suite_results`` (:mod:`examlops.evaluation`), and project membership is
``project_resources`` (ADR 0086).

Layout mirrors :mod:`examlops.agent_versions`: ``manifest`` is pure validation + content hashing,
``store`` is publish/read over :mod:`examlops.data.catalog`, ``pull`` is the Phase 2 materializer.
"""

from __future__ import annotations

from examlops.catalog.manifest import (
    KINDS,
    RESOURCE_HINTS,
    SOURCE_KINDS,
    TRUST_TIERS,
    TRUST_UNPINNED,
    CatalogEntry,
    CatalogEntryError,
    canonical_json,
    entry_hash_of,
    normalize,
    pinned_ref_problem,
)
from examlops.catalog.pull import CatalogPullError, PullResult, pull_entry, render_model_yaml
from examlops.catalog.store import (
    CatalogSignatureError,
    get_entry,
    latest_version,
    list_entries,
    list_pulls,
    publish_entry,
    resolve_ref,
    verify_entry_signature,
)

__all__ = [
    "KINDS",
    "RESOURCE_HINTS",
    "SOURCE_KINDS",
    "TRUST_TIERS",
    "TRUST_UNPINNED",
    "CatalogEntry",
    "CatalogEntryError",
    "CatalogPullError",
    "CatalogSignatureError",
    "PullResult",
    "canonical_json",
    "entry_hash_of",
    "get_entry",
    "latest_version",
    "list_entries",
    "list_pulls",
    "normalize",
    "pinned_ref_problem",
    "publish_entry",
    "pull_entry",
    "render_model_yaml",
    "resolve_ref",
    "verify_entry_signature",
]
