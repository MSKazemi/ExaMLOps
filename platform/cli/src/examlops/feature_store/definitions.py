"""Feature-view definitions declared by the use-case pack (ADR 0017 clauses 2 and 5).

A pack keeps its views in ``<pack>/features/*.yaml`` (one view per file), next to its
``models/``. Those files are the source of truth: training's feature gate and serving's
``FeatureTransformer`` both read them directly — serving needs no database for the *definition*
— and :func:`sync_definitions` mirrors them into the ``feature_views`` registry so the CLI,
materialization and the dashboard see the same view.

The platform names no concrete view; it reads whatever the pack declares (ADR 0094).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from examlops.feature_store.spec import FeatureDefinitionError, ViewDefinition

logger = logging.getLogger(__name__)

#: Refuse a definition file larger than this — a definition is a few hundred bytes.
MAX_DEFINITION_BYTES = 256 * 1024
#: At most this many view files are read from one directory.
MAX_DEFINITIONS = 500


@dataclass
class DefinitionSet:
    """Every view a directory declares, plus every file that failed to parse (never dropped)."""

    directory: Path | None
    views: dict[str, ViewDefinition] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def serving_view(self) -> ViewDefinition | None:
        """The pack's default serving view: ``EXAMLOPS_SERVING_FEATURE_VIEW`` or the one view
        marked ``serving: true``. Two views both marked is a definition error, reported by
        :func:`load_definitions`, and yields ``None`` here rather than a guess."""
        wanted = os.getenv("EXAMLOPS_SERVING_FEATURE_VIEW", "").strip()
        if wanted:
            return self.views.get(wanted)
        marked = [v for v in self.views.values() if v.serving]
        return marked[0] if len(marked) == 1 else None


def resolve_dir(directory: str | Path | None = None) -> Path | None:
    if directory is not None:
        return Path(directory)
    from examlops.usecase import features_dir

    return features_dir()


def load_definitions(directory: str | Path | None = None) -> DefinitionSet:
    """Parse every ``*.yaml`` view under ``directory`` (default: the active pack's).

    A missing directory is an empty set (a pack need not declare features). A malformed file is
    recorded in ``errors`` and its view is omitted — callers that *act* on definitions
    (:func:`sync_definitions`, the training gate) refuse to proceed while ``errors`` is non-empty.
    """
    path = resolve_dir(directory)
    out = DefinitionSet(directory=path)
    if path is None or not path.is_dir():
        return out
    import yaml

    files = sorted(p for p in path.glob("*.yaml") if not p.name.startswith("_"))
    if len(files) > MAX_DEFINITIONS:
        out.errors.append(f"{path}: {len(files)} definition files exceeds cap {MAX_DEFINITIONS}")
        files = files[:MAX_DEFINITIONS]
    for f in files:
        try:
            if f.stat().st_size > MAX_DEFINITION_BYTES:
                raise FeatureDefinitionError(f"larger than {MAX_DEFINITION_BYTES} bytes")
            raw = yaml.safe_load(f.read_text()) or {}
            view = ViewDefinition.from_dict(raw, origin=str(f))
        except (FeatureDefinitionError, yaml.YAMLError, OSError) as exc:
            out.errors.append(f"{f.name}: {exc}")
            continue
        if view.name in out.views:
            out.errors.append(f"{f.name}: view '{view.name}' is already declared elsewhere")
            continue
        out.views[view.name] = view
    marked = sorted(v.name for v in out.views.values() if v.serving)
    if len(marked) > 1:
        out.errors.append(f"more than one view is marked serving: true ({', '.join(marked)})")
    return out


def _registry_matches(view: ViewDefinition, row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    spec = row.get("spec") or {}
    return (
        spec.get("fingerprint") == view.fingerprint()
        and spec.get("entity_key") == view.entity_key
        and spec.get("entity_column") == view.entity_column
        and spec.get("timestamp_field") == view.timestamp_field
        and bool(spec.get("serving")) == view.serving
        and row.get("entity") == view.entity
        and (row.get("source") or None) == view.source
        and int(row.get("ttl_seconds") or 0) == view.ttl_seconds
        and int(row.get("materialize_interval_seconds") or 0) == view.materialize_interval_seconds
        and (row.get("embedding_feature") or None) == view.embedding_feature
    )


def sync_definitions(
    directory: str | Path | None = None,
    *,
    dry_run: bool = False,
    actor: str | None = None,
) -> dict[str, Any]:
    """Mirror the pack's view definitions into the ``feature_views`` registry. Idempotent.

    Returns ``{"directory", "views": [{"name", "action", "fingerprint"}], "orphans", "errors"}``
    where ``action`` is ``created`` / ``updated`` / ``unchanged``. Refuses (``ValueError``) while
    any definition file is malformed — applying the good half of a broken set would leave
    training and serving reading a registry nobody declared. Views in the registry but not in
    the pack are reported as ``orphans`` and never deleted: they may have been applied by hand.
    Every applied change writes a ``feature_view_synced`` audit event.
    """
    from examlops import data as platform_db
    from examlops.feature_store import FeatureView, apply_view

    defs = load_definitions(directory)
    if defs.errors:
        raise ValueError("feature definitions are invalid: " + "; ".join(defs.errors))
    results: list[dict[str, Any]] = []
    for name in sorted(defs.views):
        view = defs.views[name]
        existing = platform_db.get_feature_view(name)
        if _registry_matches(view, existing):
            action = "unchanged"
        else:
            action = "updated" if existing else "created"
            if not dry_run:
                apply_view(
                    FeatureView(
                        name=view.name,
                        entity=view.entity,
                        features=view.feature_names,
                        source=view.source,
                        ttl_seconds=view.ttl_seconds,
                        embedding_feature=view.embedding_feature,
                        spec=view.spec_dict(),
                        materialize_interval_seconds=view.materialize_interval_seconds,
                    )
                )
                _audit(
                    "feature_view_synced",
                    name,
                    {"action": action, "fingerprint": view.fingerprint(), "origin": view.origin},
                    actor,
                )
        results.append({"name": name, "action": action, "fingerprint": view.fingerprint()})
    registered = {v["name"] for v in platform_db.list_feature_views()}
    return {
        "directory": str(defs.directory) if defs.directory else None,
        "dry_run": dry_run,
        "views": results,
        "orphans": sorted(registered - set(defs.views)),
        "errors": [],
    }


def check_model_bindings(
    bindings: list[tuple[str, str, str]], definitions: DefinitionSet | None = None
) -> list[str]:
    """Problems with ``(model, dataset, view)`` bindings.

    Each named view must be declared, and must be the serving view when the pack has one:
    serving's ``FeatureTransformer`` applies that single view to every request, so a model trained
    against any other view would be served under a definition its training was never checked
    against. Guarded by ``tests/unit/test_feature_store_definitions.py`` over the reference pack.
    """
    defs = definitions if definitions is not None else load_definitions()
    problems = list(defs.errors)
    serving = defs.serving_view()
    for model, dataset, view in bindings:
        if view not in defs.views:
            problems.append(
                f"{model}/{dataset}: feature_view '{view}' is not declared under "
                f"{defs.directory or '<no features dir>'}"
            )
        elif serving is not None and view != serving.name:
            problems.append(
                f"{model}/{dataset}: trains on feature_view '{view}' but serving applies the "
                f"serving view '{serving.name}' to every request"
            )
    return problems


def _audit(action: str, target: str, details: dict[str, Any], actor: str | None) -> None:
    try:
        from examlops.data.audit import audit_best_effort

        audit_best_effort(
            "feature-store",
            actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER"),
            action,
            target,
            details,
        )
    except Exception as exc:  # noqa: BLE001 - audit_best_effort already counts a loss
        logger.warning("feature-store audit skipped for %s %s: %s", action, target, exc)


__all__ = [
    "DefinitionSet",
    "check_model_bindings",
    "load_definitions",
    "resolve_dir",
    "sync_definitions",
]
