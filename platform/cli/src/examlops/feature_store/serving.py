"""Serving side of the single train/serve feature definition (ADR 0017 clause 2).

``FeatureTransformer`` calls :func:`serving_features` instead of carrying its own copy of the
transform. The definition is the pack's serving view (``features/*.yaml`` marked
``serving: true``, or ``EXAMLOPS_SERVING_FEATURE_VIEW``), read from the file — the same file
training's feature gate validates the training set against — and cached for
``EXAMLOPS_SERVING_FEATURE_TTL`` seconds so an edited definition is picked up without a restart.

When a request omits a required feature but names its entity (the view's ``entity_key``, e.g.
``job_id``), the materialized online value is looked up (``EXAMLOPS_SERVING_ONLINE_FEATURES``,
on by default) — the value training's point-in-time retrieval would have produced, so the model
sees the same vector either way. The lookup happens only on that path: a request that carries its
features costs no store read.

With no resolvable definition (no pack mounted, a malformed file) the caller's legacy transform is
used and :func:`resolution` says why, so a broken pack degrades to the previous behaviour rather
than taking inference down.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from examlops.feature_store.spec import FeatureValidationError, ViewDefinition, transform_row

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resolution:
    """Which definition serving is using right now, and why."""

    view: ViewDefinition | None
    reason: str

    @property
    def fingerprint(self) -> str | None:
        return self.view.fingerprint() if self.view else None


_lock = threading.Lock()
_state: dict[str, Any] = {"resolution": None, "expires": 0.0}


def _ttl() -> float:
    try:
        return max(0.0, float(os.getenv("EXAMLOPS_SERVING_FEATURE_TTL", "60")))
    except ValueError:
        return 60.0


def reset_cache() -> None:
    with _lock:
        _state["resolution"] = None
        _state["expires"] = 0.0


def resolution() -> Resolution:
    """The serving view in force (cached), or ``Resolution(None, reason)``. Never raises."""
    now = time.monotonic()
    with _lock:
        cached = _state["resolution"]
        if cached is not None and now < _state["expires"]:
            return cached  # type: ignore[no-any-return]
    try:
        from examlops.feature_store.definitions import load_definitions

        defs = load_definitions()
        if defs.errors:
            res = Resolution(None, "feature definitions invalid: " + "; ".join(defs.errors))
        else:
            view = defs.serving_view()
            if view is None:
                res = Resolution(None, f"no serving feature view under {defs.directory}")
            else:
                res = Resolution(view, f"{view.origin or view.name}")
    except Exception as exc:  # noqa: BLE001 - serving must degrade, not fail
        res = Resolution(None, f"feature definitions unreadable: {type(exc).__name__}: {exc}")
    previous = _state["resolution"]
    if res.view is None and (previous is None or previous.reason != res.reason):
        logger.warning("serving features use the legacy transform: %s", res.reason)
    with _lock:
        _state["resolution"] = res
        _state["expires"] = now + _ttl()
    return res


def _online_lookup_enabled() -> bool:
    return os.getenv("EXAMLOPS_SERVING_ONLINE_FEATURES", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _missing_required(view: ViewDefinition, req: dict[str, Any]) -> bool:
    return any(spec.required and req.get(spec.name) is None for spec in view.features)


def request_required_fields(view: ViewDefinition, req: dict[str, Any]) -> list[str]:
    """The view's features an inference request must itself carry.

    None when the request names its entity and online lookup is on — :func:`serving_features`
    then reads them from the online store — else every required feature. The ingress presence
    check derives its field list from this, so it cannot refuse the request the transform was
    built to serve, and it carries no second copy of the feature list.
    """
    if view.entity_key and req.get(view.entity_key) is not None and _online_lookup_enabled():
        return []
    return [spec.name for spec in view.features if spec.required]


def serving_features(view: ViewDefinition, req: dict[str, Any]) -> dict[str, Any]:
    """The model's feature dict for an inference request, under ``view``.

    Raises :class:`FeatureValidationError` (a ``ValueError``) exactly where training's gate would
    reject the same row.
    """
    row = req
    if (
        _missing_required(view, req)
        and view.entity_key
        and req.get(view.entity_key) is not None
        and _online_lookup_enabled()
    ):
        entity_id = str(req[view.entity_key])
        try:
            from examlops.feature_store.online import select_online_store

            online = select_online_store().read(view.name, entity_id)
        except Exception as exc:  # noqa: BLE001 - surfaced as a validation error below
            logger.warning("online feature lookup failed for %s/%s: %s", view.name, entity_id, exc)
            online = None
        if online:
            # Request values win over stored ones: the caller's explicit input is the newer fact.
            row = {**online, **{k: v for k, v in req.items() if v is not None}}
    try:
        return transform_row(view, row)
    except FeatureValidationError as exc:
        if row is req and view.entity_key and req.get(view.entity_key) is not None:
            raise FeatureValidationError(
                f"{exc} (no materialized online features for "
                f"{view.entity_key}={req[view.entity_key]} in view '{view.name}')"
            ) from exc
        raise


__all__ = [
    "Resolution",
    "request_required_fields",
    "reset_cache",
    "resolution",
    "serving_features",
]
