"""Idempotency keys for mutating agent calls (ADR 0147 decision 4).

An agent that retries a write — after a timeout, or after its runtime re-executes a node following
a crash — must not act twice. A mutating tool accepts an optional ``idempotency_key``:

* first call with a key: the key is **claimed atomically**, the tool runs, and its successful
  result is stored against the key;
* same key + same request: the **original result** comes back with ``replayed: true`` and the
  tool does not run again;
* same key + a *different* request: refused with ``idempotency_conflict`` and never applied;
* same key while the first call is still in flight: refused with ``idempotency_in_progress``
  (a concurrent double-submit cannot run twice; the caller retries and gets the replay).

A failed call (``ok`` falsy, or one that raised) releases its claim, so the retry of a call that
did nothing is not blocked. A claim whose holder crashed mid-call expires after
``EXAMLOPS_IDEMPOTENCY_PENDING_TTL`` seconds; a stored result expires after
``EXAMLOPS_IDEMPOTENCY_TTL`` seconds (default 24 h).

The request hash covers the tool name and every bound argument except the key itself. Keys are
global, not per caller: pick an unguessable one (a UUID). Storage is ``platform_db`` — the claim is
one conditional insert inside an immediate-write transaction, the same discipline as
``claim_drift_trigger``.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
from collections.abc import Callable
from typing import Any

from examlops.data.idempotency import claim, finish, init_db, release

__all__ = ["DEFAULT_TTL_S", "PARAM", "idempotent", "run_idempotent"]

PARAM = "idempotency_key"
DEFAULT_TTL_S = 86400.0
DEFAULT_PENDING_TTL_S = 300.0
_MAX_KEY_LEN = 200


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def ttl_seconds() -> float:
    return _env_seconds("EXAMLOPS_IDEMPOTENCY_TTL", DEFAULT_TTL_S)


def pending_ttl_seconds() -> float:
    return _env_seconds("EXAMLOPS_IDEMPOTENCY_PENDING_TTL", DEFAULT_PENDING_TTL_S)


def request_hash(scope: str, request: dict[str, Any]) -> str:
    blob = json.dumps({"scope": scope, "request": request}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _err(code: str, message: str, key: str) -> dict[str, Any]:
    return {"ok": False, "error": message, "code": code, "idempotency_key": key}


def run_idempotent(
    scope: str, key: str | None, request: dict[str, Any], fn: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Run ``fn`` at most once per ``key`` (see module docstring). ``key`` empty = just run."""
    if not key:
        return fn()
    if len(key) > _MAX_KEY_LEN:
        return _err("idempotency_invalid", f"idempotency_key longer than {_MAX_KEY_LEN}", key[:40])
    digest = request_hash(scope, request)
    try:
        init_db()
        existing = claim(key, scope, digest, pending_ttl_seconds())
    except Exception as exc:  # noqa: BLE001 - never act unguarded when the key cannot be honoured
        return _err("idempotency_unavailable", f"idempotency store unavailable: {exc}", key)
    if existing is not None:
        if existing.get("request_hash") != digest:
            return _err(
                "idempotency_conflict",
                "idempotency_key was already used for a different request; not applied",
                key,
            )
        if existing.get("state") == "done" and existing.get("result_json"):
            return {**json.loads(existing["result_json"]), "replayed": True}
        return _err(
            "idempotency_in_progress",
            "a call with this idempotency_key is still in progress; retry shortly",
            key,
        )
    try:
        result = fn()
    except BaseException:
        release(key)
        raise
    try:
        if isinstance(result, dict) and result.get("ok"):
            finish(key, result, ttl_seconds())
        else:
            release(key)
    except Exception:  # noqa: BLE001 - the action already happened; do not report it as failed
        pass
    return result


def idempotent(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Give a mutating tool an optional trailing ``idempotency_key`` argument."""
    sig = inspect.signature(fn)
    if PARAM in sig.parameters:
        return fn
    scope = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args: Any, idempotency_key: str | None = None, **kwargs: Any) -> dict[str, Any]:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return run_idempotent(
            scope, idempotency_key, dict(bound.arguments), lambda: fn(*bound.args, **bound.kwargs)
        )

    params = [
        *sig.parameters.values(),
        inspect.Parameter(
            PARAM, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None
        ),
    ]
    wrapper.__signature__ = sig.replace(parameters=params)  # type: ignore[attr-defined]
    # Introspectors (pydantic validate_arguments / LangChain StructuredTool) read the function's
    # own annotations, not __signature__: without this entry they raise KeyError.
    wrapper.__annotations__ = {**getattr(wrapper, "__annotations__", {}), PARAM: str | None}
    wrapper.__doc__ = (fn.__doc__ or "").rstrip() + (
        "\n\n    Args (added):\n        idempotency_key: Optional unique key; a repeat with the same"
        " key and request returns the original result (``replayed: true``), a different request"
        " under the same key is refused."
    )
    return wrapper
