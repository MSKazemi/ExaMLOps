"""ADR 0035 clause 3 — structured-output & reasoning **policy**.

Three things the ADR names under *Policy* and that previously had no configuration surface:

1. **Default schemas for common platform outputs.** :data:`BUILTIN_SCHEMAS` ships the shapes the
   platform itself produces — a tool call, a cited RAG answer, an extraction, a classification —
   so a caller asks for ``response_schema="rag_answer"`` instead of re-typing (and drifting) a
   JSON Schema. A site adds its own under ``schemas:`` in ``structured.yaml``; it may **not**
   redefine a built-in name, because a caller that asked for ``tool_call`` must get the shape the
   platform documents, not one a config file quietly changed.
2. **Per-route defaults.** ``routes:`` maps a gateway route (the logical model name a caller sends,
   or an ``fnmatch`` glob over it) to a default ``response_schema`` and/or ``reasoning_budget``.
   A route budget is one more candidate in :func:`examlops.structured.resolve_reasoning_budget`,
   where the tightest cap wins — a route default can tighten a key/project/model cap, never lift it.
3. **Budgets gate via D5** (policy-as-code). :func:`reasoning_request_decision` consults
   ``examlops.policy`` for the action ``reasoning_request`` before a request reaches any backend,
   with the resolved budget in the context, so a site can write e.g.::

       policies:
         - action: reasoning_request
           when: "not has_budget"
           effect: deny            # no unbounded thinking on this platform

   and :func:`budget_change_decision` consults ``reasoning_budget_set`` before a cap is written.

The file is ``<config dir>/structured.yaml`` (``EXAMLOPS_STRUCTURED_CONFIG`` overrides the path;
config dir per ADR 0128). It is validated **totally** — every problem reported at once with its key
path — by :func:`validate_config`; the same validator backs ``exa gateway schema list``. At request
time a file that does not validate is ignored as a whole (logged, never half-applied): applying the
half that parsed would make which defaults are in force depend on where the typo was.
"""

from __future__ import annotations

import copy
import fnmatch
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("examlops.structured.policy")

ENV_PATH = "EXAMLOPS_STRUCTURED_CONFIG"
#: D5 actions this module consults (``policy.yaml`` ``action:`` values).
ACTION_REQUEST = "reasoning_request"
ACTION_BUDGET_SET = "reasoning_budget_set"

#: The platform's own output shapes (clause 3). Kept deliberately small and strict: every one is
#: an object with ``additionalProperties: false`` so a repair drops what a model invents.
BUILTIN_SCHEMAS: dict[str, dict[str, Any]] = {
    "tool_call": {
        "type": "object",
        "description": "One agent tool invocation: the tool's name and its JSON arguments.",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "arguments": {"type": "object"},
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
    },
    "rag_answer": {
        "type": "object",
        "description": "A RAG answer and the 1-based numbers of the context chunks it relies on.",
        "properties": {
            "answer": {"type": "string"},
            # No ``minimum``: a number outside the context is checked against the chunks
            # actually retrieved and dropped there, which a schema bound cannot know.
            "citations": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["answer", "citations"],
        "additionalProperties": False,
    },
    "extraction": {
        "type": "object",
        "description": "Named fields extracted from a document, with an overall confidence.",
        "properties": {
            "fields": {"type": "object"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["fields"],
        "additionalProperties": False,
    },
    "classification": {
        "type": "object",
        "description": "A single label with a confidence and a short rationale.",
        "properties": {
            "label": {"type": "string", "minLength": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string"},
        },
        "required": ["label"],
        "additionalProperties": False,
    },
}

#: Hard cap on a configured reasoning budget; a value above it is a typo, not a policy.
MAX_BUDGET_TOKENS = 10_000_000
_MAX_SCHEMAS = 256
_MAX_ROUTES = 1024


class UnknownSchemaError(KeyError):
    """A schema name that is neither built in nor defined in ``structured.yaml``."""

    def __str__(self) -> str:  # KeyError quotes its arg; this reads as a sentence
        return str(self.args[0]) if self.args else "unknown schema"


@dataclass(frozen=True)
class RouteDefaults:
    """What ``structured.yaml`` says about one route; ``None`` fields mean "no default"."""

    response_schema: str | None = None
    reasoning_budget: int | None = None
    matched: tuple[str, ...] = ()


@dataclass
class StructuredConfig:
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    routes: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: str | None = None


def config_path() -> Path:
    raw = os.getenv(ENV_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    from examlops.lifecycle.datadir import config_dir

    return config_dir() / "structured.yaml"


def _check_schema(schema: Any) -> list[str]:
    if not isinstance(schema, Mapping):
        return ["must be a JSON Schema object (a mapping)"]
    try:
        import jsonschema

        try:
            jsonschema.Draft202012Validator.check_schema(dict(schema))
        except jsonschema.SchemaError as exc:
            return [f"not a valid JSON Schema: {exc.message}"]
    except ImportError:  # the built-in validator has nothing stricter to say
        if "type" in schema and not isinstance(schema["type"], str | list):
            return ["'type' must be a string or list"]
    return []


def validate_config(raw: Any) -> list[str]:
    """Every problem in a parsed ``structured.yaml``, each prefixed with its key path."""
    errors: list[str] = []
    if raw is None:
        return errors
    if not isinstance(raw, Mapping):
        return ["<root>: must be a mapping with 'schemas:' and/or 'routes:'"]
    unknown = sorted(set(raw) - {"version", "schemas", "routes"})
    errors += [f"{k}: unknown top-level key" for k in unknown]
    if raw.get("version", 1) != 1:
        errors.append("version: only version 1 is supported")
    schemas = raw.get("schemas") or {}
    if not isinstance(schemas, Mapping):
        errors.append("schemas: must be a mapping of name -> JSON Schema")
        schemas = {}
    if len(schemas) > _MAX_SCHEMAS:
        errors.append(f"schemas: at most {_MAX_SCHEMAS} schemas")
    for name, schema in schemas.items():
        if not isinstance(name, str) or not name.strip():
            errors.append(f"schemas.{name!r}: name must be a non-empty string")
            continue
        if name in BUILTIN_SCHEMAS:
            errors.append(f"schemas.{name}: redefines a built-in schema (choose another name)")
            continue
        errors += [f"schemas.{name}: {e}" for e in _check_schema(schema)]
    known = set(BUILTIN_SCHEMAS) | {n for n in schemas if isinstance(n, str)}
    routes = raw.get("routes") or {}
    if not isinstance(routes, Mapping):
        errors.append("routes: must be a mapping of route (or glob) -> defaults")
        routes = {}
    if len(routes) > _MAX_ROUTES:
        errors.append(f"routes: at most {_MAX_ROUTES} routes")
    for route, spec in routes.items():
        at = f"routes.{route}"
        if not isinstance(route, str) or not route.strip():
            errors.append(f"routes.{route!r}: route must be a non-empty string")
            continue
        if not isinstance(spec, Mapping) or not spec:
            errors.append(f"{at}: must set response_schema and/or reasoning_budget")
            continue
        extra = sorted(set(spec) - {"response_schema", "reasoning_budget"})
        errors += [f"{at}.{k}: unknown key" for k in extra]
        if "response_schema" in spec:
            ref = spec["response_schema"]
            if not isinstance(ref, str) or ref not in known:
                errors.append(f"{at}.response_schema: unknown schema {ref!r}")
        if "reasoning_budget" in spec:
            b = spec["reasoning_budget"]
            if isinstance(b, bool) or not isinstance(b, int) or not 0 <= b <= MAX_BUDGET_TOKENS:
                errors.append(
                    f"{at}.reasoning_budget: must be an integer in [0, {MAX_BUDGET_TOKENS}]"
                )
    return errors


class StructuredConfigError(ValueError):
    def __init__(self, errors: list[str], path: str | None = None) -> None:
        self.errors = errors
        where = f" ({path})" if path else ""
        super().__init__(f"invalid structured.yaml{where}:\n  " + "\n  ".join(errors))


def load_config(path: str | Path | None = None) -> StructuredConfig:
    """Parse + validate the file. Missing ⇒ empty config; invalid ⇒ :class:`StructuredConfigError`."""
    p = Path(path) if path is not None else config_path()
    if not p.is_file():
        return StructuredConfig(path=str(p))
    import yaml

    try:
        raw = yaml.safe_load(p.read_text())
    except Exception as exc:  # noqa: BLE001 - a parse error is a config error, reported as one
        raise StructuredConfigError([f"<root>: not valid YAML: {exc}"], str(p)) from exc
    errors = validate_config(raw)
    if errors:
        raise StructuredConfigError(errors, str(p))
    raw = raw or {}
    return StructuredConfig(
        schemas={k: dict(v) for k, v in (raw.get("schemas") or {}).items()},
        routes={k: dict(v) for k, v in (raw.get("routes") or {}).items()},
        path=str(p),
    )


# Cached by (path, mtime_ns, size): the gateway resolves defaults on every request, and re-parsing
# YAML per request would be wasteful; an edited file is picked up on the next request.
_cache_lock = threading.Lock()
_cache: tuple[tuple[str, int, int], StructuredConfig] | None = None


def active_config() -> StructuredConfig:
    """The config in force for request handling. An invalid file is ignored **as a whole**."""
    global _cache
    p = config_path()
    try:
        st = p.stat()
    except OSError:
        return StructuredConfig(path=str(p))
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
    try:
        cfg = load_config(p)
    except StructuredConfigError as exc:
        log.warning("%s — no structured-output defaults are applied until it is fixed", exc)
        cfg = StructuredConfig(path=str(p))
    with _cache_lock:
        _cache = (key, cfg)
    return cfg


def list_schemas(cfg: StructuredConfig | None = None) -> list[dict[str, Any]]:
    cfg = cfg if cfg is not None else active_config()
    rows = [
        {"name": n, "source": "builtin", "description": s.get("description", "")}
        for n, s in BUILTIN_SCHEMAS.items()
    ]
    rows += [
        {"name": n, "source": "site", "description": str(s.get("description", ""))}
        for n, s in sorted(cfg.schemas.items())
    ]
    return rows


def get_schema(name: str, cfg: StructuredConfig | None = None) -> dict[str, Any]:
    """A deep copy of the named schema (callers may mutate it). Raises :class:`UnknownSchemaError`."""
    if name in BUILTIN_SCHEMAS:
        return copy.deepcopy(BUILTIN_SCHEMAS[name])
    cfg = cfg if cfg is not None else active_config()
    if name in cfg.schemas:
        return copy.deepcopy(cfg.schemas[name])
    known = ", ".join(sorted(set(BUILTIN_SCHEMAS) | set(cfg.schemas)))
    raise UnknownSchemaError(f"unknown schema {name!r} (known: {known})")


def route_defaults(route: str, cfg: StructuredConfig | None = None) -> RouteDefaults:
    """Defaults for one route: an exact entry first, then globs in file order.

    The schema comes from the first entry that sets one. The budget is the **tightest** of every
    matching entry, the same rule the budget resolver applies across scopes.
    """
    cfg = cfg if cfg is not None else active_config()
    entries: list[tuple[str, dict[str, Any]]] = []
    if route in cfg.routes:
        entries.append((route, cfg.routes[route]))
    for pattern, spec in cfg.routes.items():
        if pattern != route and fnmatch.fnmatchcase(route, pattern):
            entries.append((pattern, spec))
    schema = next((str(s["response_schema"]) for _, s in entries if "response_schema" in s), None)
    budgets = [int(s["reasoning_budget"]) for _, s in entries if "reasoning_budget" in s]
    return RouteDefaults(
        response_schema=schema,
        reasoning_budget=min(budgets) if budgets else None,
        matched=tuple(p for p, _ in entries),
    )


def resolve_response_schema(
    route: str, requested: dict[str, Any] | str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """``(schema, name)`` for a request: the caller's own, a named one, or the route default.

    A caller-supplied dict always wins (it is the most specific statement of what they want). A
    name resolves through the registry and raises :class:`UnknownSchemaError` if it is not there —
    before any backend is called, so a typo never costs a model call.
    """
    if isinstance(requested, dict):
        return requested, None
    if isinstance(requested, str):
        return get_schema(requested), requested
    if requested is not None:
        raise TypeError("response_schema must be a JSON Schema dict or a schema name")
    name = route_defaults(route).response_schema
    return (get_schema(name), name) if name else (None, None)


# -- D5 gates -------------------------------------------------------------------------------


def reasoning_request_decision(
    route: str,
    *,
    tenant: str,
    project: str | None,
    key_hash: str | None,
    budget_tokens: int | None,
    budget_source: str | None,
) -> Any:
    """Consult D5 (``policy.yaml`` action ``reasoning_request``) for one gateway request.

    Evaluated with ``audit=False`` and recorded only when a rule actually matched, so a platform
    with no such rule keeps its request path free of per-request audit rows. ``require_approval``
    cannot be satisfied inside a request, so callers treat anything but ``allow`` as a refusal. An
    engine that *raises* denies (``decide_safe``) — a broken policy engine never grants a request.
    """
    from examlops import policy

    ctx = {
        "model": route,
        "route": route,
        "tenant": tenant,
        "project": project or "",
        "key_hash": key_hash or "",
        "has_budget": budget_tokens is not None,
        "budget_tokens": budget_tokens if budget_tokens is not None else -1,
        "budget_source": budget_source or "none",
    }
    decision = policy.decide_safe(ACTION_REQUEST, ctx, audit=False)
    if decision.rule is not None or decision.shadow:
        policy.record_decision(ACTION_REQUEST, ctx, decision)
    return decision


def budget_change_decision(
    scope: str, ref: str, *, tenant: str, max_thinking_tokens: int | None, remove: bool
) -> Any:
    """Consult D5 (action ``reasoning_budget_set``) before a budget is written or removed."""
    from examlops import policy

    ctx = {
        "scope": scope,
        "target": f"{scope}:{ref}",
        "ref": ref,
        "tenant": tenant,
        # A removal sets no cap: -1, as for "none", so a ``max_thinking_tokens > N`` ceiling rule
        # neither refuses a removal on the (ignored) positional value nor reads it as a new cap.
        "max_thinking_tokens": (
            max_thinking_tokens if max_thinking_tokens is not None and not remove else -1
        ),
        "op": "remove" if remove else "set",
    }
    return policy.decide_safe(ACTION_BUDGET_SET, ctx)


__all__ = [
    "ACTION_BUDGET_SET",
    "ACTION_REQUEST",
    "BUILTIN_SCHEMAS",
    "RouteDefaults",
    "StructuredConfig",
    "StructuredConfigError",
    "UnknownSchemaError",
    "active_config",
    "budget_change_decision",
    "config_path",
    "get_schema",
    "list_schemas",
    "load_config",
    "reasoning_request_decision",
    "resolve_response_schema",
    "route_defaults",
    "validate_config",
]
