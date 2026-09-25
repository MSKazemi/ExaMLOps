"""Declarative guardrail policy engine (ADR 0026 clause 3).

The decision this implements: *compose checks via an OSS framework behind a ``Guardrail``
interface; declarative, per-route/per-tenant, with modes ``off | monitor | enforce``.*

A policy file (YAML) names, for each direction, an ordered list of **checks**. A check is either
one of the built-in detectors that :class:`examlops.guardrails.DefaultGuardrail` already runs
(``injection``, ``pii``, ``secret``, ``toxicity``) plus two policy-only ones (``topics``,
``length``), a framework adapter (``llm_guard`` — LLM-Guard's scanners, lazily imported from the
``llm-guard`` package in a separate guardrail image, see :mod:`examlops.guardrails.frameworks`), or a
third-party check registered under the ``exa.guardrails.checks`` entry-point group (how NeMo
Guardrails or Guardrails AI plug in without a core change).

Resolution is layered, later wins, whole-value replacement (lists are never merged, so what a
layer says is exactly what applies)::

    default  →  tenants[<tenant>]  →  every routes[] entry whose `match` glob fits the route
                                      (and whose optional `tenant` glob fits the tenant), in order

:class:`PolicyGuardrail` implements the :class:`~examlops.guardrails.Guardrail` protocol, so it
drops into every boundary the default guardrail already guards (the B2 gateway client, the
``llm-gateway`` service, RAG, ``exa guardrails test``) with no call-site change beyond passing the
route. The policy file is re-read when its mtime changes, so an operator edit takes effect
without a restart.

Failure semantics — the part that makes this a *gate* rather than a report:

* **enforce fails closed.** A check that raises, times out, or cannot be constructed (e.g. the
  LLM-Guard extra is not installed) blocks the text with a ``scanner-error:<check>`` /
  ``check-unavailable:<check>`` finding. ``monitor`` records the same finding and lets the text
  through; ``off`` runs nothing.
* **An unreadable or invalid policy file never leaves the boundary unscanned.** It degrades to the
  built-in :class:`DefaultGuardrail` at ``EXAMLOPS_GUARDRAIL_MODE`` (exactly the pre-policy
  behaviour), logs the errors, and audits ``guardrail_policy_invalid`` once per file version.
  ``exa guardrails policy validate`` exits non-zero on the same errors so CI catches them first.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("examlops.guardrails.policy")

MODES = ("off", "monitor", "enforce")
ACTIONS = ("block", "redact", "flag")
DIRECTIONS = ("input", "output")
POLICY_VERSION = 1

#: Policy files are small hand-written YAML; anything bigger is a mistake or an attack.
MAX_POLICY_BYTES = 256 * 1024
MAX_CHECKS_PER_DIRECTION = 32
MAX_TOPICS = 256
MAX_ROUTES = 256
#: Upper bound on any one check's wall-clock budget, whatever the policy says.
MAX_CHECK_TIMEOUT_S = 60.0
DEFAULT_FRAMEWORK_TIMEOUT_S = 10.0

_LAYER_KEYS = frozenset(
    {"mode", "input", "output", "banned_topics", "allowed_tools", "blocked_tools"}
)
_TOP_KEYS = frozenset({"version", "default", "tenants", "routes"})
_CHECK_KEYS = frozenset(
    {"check", "action", "timeout_s", "max_chars", "scanners", "fail_fast", "config"}
)


class PolicyError(ValueError):
    """A policy file that cannot be applied; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# ── check contract ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckOutcome:
    """What one check found. ``text`` is the check's sanitised version (== input if none)."""

    findings: tuple[str, ...] = ()
    text: str | None = None
    #: The check itself demands a block regardless of its configured action (e.g. a length cap).
    hard_block: bool = False


class Check(Protocol):
    """One composable guardrail check. Implementations must be thread-safe."""

    name: str

    def run(self, text: str, direction: str, ctx: dict[str, Any]) -> CheckOutcome: ...


@dataclass(frozen=True)
class CheckSpec:
    """One entry of a policy's ``input:``/``output:`` list, after validation."""

    check: str
    action: str
    timeout_s: float | None = None
    params: tuple[tuple[str, Any], ...] = ()

    def param(self, key: str, default: Any = None) -> Any:
        return dict(self.params).get(key, default)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"check": self.check, "action": self.action}
        if self.timeout_s is not None:
            out["timeout_s"] = self.timeout_s
        out.update(dict(self.params))
        return out


@dataclass(frozen=True)
class _CheckKind:
    default_action: str
    allowed_actions: tuple[str, ...]
    factory: Callable[[CheckSpec, ResolvedPolicy], Check]
    framework: bool = False
    #: Import probe: returns ``None`` when available, else a reason string.
    availability: Callable[[], str | None] | None = None
    description: str = ""


# ── built-in checks (reuse the detectors the default guardrail already validates) ────────────


class _InjectionCheck:
    name = "injection"

    def run(self, text: str, direction: str, ctx: dict[str, Any]) -> CheckOutcome:
        from examlops.guardrails import _INJECTION

        return CheckOutcome(("injection",)) if _INJECTION.search(text) else CheckOutcome()


class _PiiCheck:
    name = "pii"

    def run(self, text: str, direction: str, ctx: dict[str, Any]) -> CheckOutcome:
        from examlops.guardrails import redact_pii

        redacted, findings = redact_pii(text)
        return CheckOutcome(tuple(findings), redacted) if findings else CheckOutcome()


class _SecretCheck:
    name = "secret"

    def run(self, text: str, direction: str, ctx: dict[str, Any]) -> CheckOutcome:
        from examlops.guardrails import _redact_secret, _secret_hit

        if not _secret_hit(text):
            return CheckOutcome()
        return CheckOutcome(("secret",), _redact_secret(text))


class _ToxicityCheck:
    name = "toxicity"

    def run(self, text: str, direction: str, ctx: dict[str, Any]) -> CheckOutcome:
        from examlops.guardrails import _TOXIC

        return CheckOutcome(("toxicity",)) if _TOXIC.search(text) else CheckOutcome()


class _TopicsCheck:
    """Deny-list of topics (ADR 0026 clause 1 "topic/allow-list policy"), whole-word, no case."""

    name = "topics"

    def __init__(self, topics: Iterable[str]):
        self._topics = [
            (t, re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.IGNORECASE)) for t in topics
        ]

    def run(self, text: str, direction: str, ctx: dict[str, Any]) -> CheckOutcome:
        hits = tuple(f"topic:{t}" for t, pat in self._topics if pat.search(text))
        return CheckOutcome(hits)


class _LengthCheck:
    name = "length"

    def __init__(self, max_chars: int):
        self.max_chars = max_chars

    def run(self, text: str, direction: str, ctx: dict[str, Any]) -> CheckOutcome:
        if len(text) > self.max_chars:
            return CheckOutcome(("length",), hard_block=True)
        return CheckOutcome()


def _llm_guard_factory(spec: CheckSpec, policy: ResolvedPolicy) -> Check:
    from examlops.guardrails.frameworks import LLMGuardCheck

    return LLMGuardCheck(
        scanners=json.loads(spec.param("scanners") or "[]"),
        fail_fast=bool(spec.param("fail_fast", True)),
    )


def _llm_guard_available() -> str | None:
    from examlops.guardrails.frameworks import llm_guard_unavailable_reason

    return llm_guard_unavailable_reason()


_BUILTINS: dict[str, _CheckKind] = {
    "injection": _CheckKind(
        "block",
        ("block", "flag"),
        lambda s, p: _InjectionCheck(),
        description="Prompt-injection / jailbreak phrases (regex).",
    ),
    "pii": _CheckKind(
        "redact",
        ACTIONS,
        lambda s, p: _PiiCheck(),
        description="PII: email/phone/SSN/card/IBAN/IP (regex) + opt-in Presidio NER.",
    ),
    "secret": _CheckKind(
        "redact",
        ACTIONS,
        lambda s, p: _SecretCheck(),
        description="Credential leak (the D7 secret scanner).",
    ),
    "toxicity": _CheckKind(
        "block",
        ("block", "flag"),
        lambda s, p: _ToxicityCheck(),
        description="Toxicity wordlist stub; use llm_guard Toxicity for a model classifier.",
    ),
    "topics": _CheckKind(
        "block",
        ("block", "flag"),
        lambda s, p: _TopicsCheck(p.banned_topics),
        description="Banned topics from the policy's banned_topics list (whole word).",
    ),
    "length": _CheckKind(
        "block",
        ("block",),
        lambda s, p: _LengthCheck(int(s.param("max_chars"))),
        description="Hard cap on text length (max_chars).",
    ),
    "llm_guard": _CheckKind(
        "block",
        ACTIONS,
        _llm_guard_factory,
        framework=True,
        availability=_llm_guard_available,
        description="LLM-Guard scanners (llm-guard, separate guardrail image).",
    ),
}

#: Entry-point group through which a third-party check (NeMo Guardrails, Guardrails AI, an
#: in-house classifier) plugs in. The entry point loads a factory ``(params: dict) -> Check``.
ENTRY_POINT_GROUP = "exa.guardrails.checks"

_registry_lock = threading.Lock()
_plugins: dict[str, _CheckKind] | None = None


def register_check(
    name: str,
    factory: Callable[[dict[str, Any]], Check],
    *,
    default_action: str = "block",
    allowed_actions: tuple[str, ...] = ACTIONS,
    description: str = "",
) -> None:
    """Register a check programmatically (tests, embedding applications)."""
    if name in _BUILTINS:
        raise ValueError(f"cannot replace built-in guardrail check {name!r}")
    if default_action not in allowed_actions:
        raise ValueError(f"default action {default_action!r} not in {allowed_actions}")
    kinds = _plugin_kinds()
    with _registry_lock:
        kinds[name] = _CheckKind(
            default_action,
            allowed_actions,
            lambda s, p: factory(json.loads(s.param("config") or "{}")),
            framework=True,
            description=description,
        )


def unregister_check(name: str) -> None:
    with _registry_lock:
        if _plugins is not None:
            _plugins.pop(name, None)


def _plugin_kinds() -> dict[str, _CheckKind]:
    global _plugins
    with _registry_lock:
        if _plugins is not None:
            return _plugins
        found: dict[str, _CheckKind] = {}
        try:
            from importlib.metadata import entry_points

            for ep in entry_points(group=ENTRY_POINT_GROUP):
                if ep.name in _BUILTINS:
                    log.warning("guardrail plugin %r shadows a built-in check; ignored", ep.name)
                    continue

                def _factory(s: CheckSpec, p: ResolvedPolicy, _ep: Any = ep) -> Check:
                    return _ep.load()(json.loads(s.param("config") or "{}"))

                found[ep.name] = _CheckKind("block", ACTIONS, _factory, framework=True)
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not break the registry
            log.warning("guardrail check entry points could not be listed: %s", exc)
        _plugins = found
        return _plugins


def check_kinds() -> dict[str, _CheckKind]:
    return {**_BUILTINS, **_plugin_kinds()}


def list_checks() -> list[dict[str, Any]]:
    """Every check a policy may name, with whether it can run in this environment."""
    rows = []
    for name, kind in sorted(check_kinds().items()):
        reason = None
        if kind.availability is not None:
            try:
                reason = kind.availability()
            except Exception as exc:  # noqa: BLE001
                reason = str(exc)
        rows.append(
            {
                "check": name,
                "framework": kind.framework,
                "default_action": kind.default_action,
                "actions": list(kind.allowed_actions),
                "available": reason is None,
                "reason": reason or "",
                "description": kind.description,
            }
        )
    return rows


# ── policy model ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedPolicy:
    """The effective policy for one (tenant, route)."""

    mode: str = "monitor"
    input: tuple[CheckSpec, ...] = ()
    output: tuple[CheckSpec, ...] = ()
    banned_topics: tuple[str, ...] = ()
    allowed_tools: frozenset[str] | None = None
    blocked_tools: frozenset[str] = frozenset()
    #: Which layers contributed, for `exa guardrails policy show` and the audit trail.
    layers: tuple[str, ...] = ("default",)

    def checks(self, direction: str) -> tuple[CheckSpec, ...]:
        return self.input if direction == "input" else self.output

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "input": [c.as_dict() for c in self.input],
            "output": [c.as_dict() for c in self.output],
            "banned_topics": list(self.banned_topics),
            "allowed_tools": sorted(self.allowed_tools) if self.allowed_tools is not None else None,
            "blocked_tools": sorted(self.blocked_tools),
            "layers": list(self.layers),
        }


#: What a policy file with no `default:` means — identical to DefaultGuardrail's own checks.
BUILTIN_DEFAULT = ResolvedPolicy(
    mode="monitor",
    input=(
        CheckSpec("injection", "block"),
        CheckSpec("pii", "redact"),
        CheckSpec("secret", "redact"),
    ),
    output=(
        CheckSpec("toxicity", "block"),
        CheckSpec("pii", "redact"),
        CheckSpec("secret", "redact"),
    ),
)


@dataclass(frozen=True)
class _Route:
    match: str
    tenant: str
    layer: dict[str, Any]


@dataclass(frozen=True)
class GuardrailPolicy:
    """A validated policy file."""

    default: dict[str, Any] = field(default_factory=dict)
    tenants: dict[str, dict[str, Any]] = field(default_factory=dict)
    routes: tuple[_Route, ...] = ()
    source: str = "<inline>"

    def resolve(
        self, tenant: str = "default", route: str | None = None, *, base_mode: str | None = None
    ) -> ResolvedPolicy:
        """Effective policy for ``(tenant, route)``.

        ``base_mode`` is the mode wherever no layer sets one — the deployment's
        ``EXAMLOPS_GUARDRAIL_MODE`` at every enforcement boundary. Without it a policy file that
        merely adds a banned topic would silently downgrade an ``enforce`` deployment to the
        built-in default's ``monitor``: adding policy must never weaken the boundary.
        """
        base = BUILTIN_DEFAULT
        if base_mode is not None and base_mode in MODES:
            base = replace(base, mode=base_mode)
        policy = _apply(base, self.default, "default")
        if tenant in self.tenants:
            policy = _apply(policy, self.tenants[tenant], f"tenant:{tenant}")
        if route:
            for i, r in enumerate(self.routes):
                if fnmatch.fnmatchcase(route, r.match) and fnmatch.fnmatchcase(tenant, r.tenant):
                    policy = _apply(policy, r.layer, f"route[{i}]:{r.match}")
        return policy


def _apply(base: ResolvedPolicy, layer: dict[str, Any], name: str) -> ResolvedPolicy:
    if not layer:
        return base
    changes: dict[str, Any] = {"layers": (*base.layers, name) if name != "default" else base.layers}
    if "mode" in layer:
        changes["mode"] = layer["mode"]
    for direction in DIRECTIONS:
        if direction in layer:
            changes[direction] = layer[direction]
    if "banned_topics" in layer:
        changes["banned_topics"] = layer["banned_topics"]
    if "allowed_tools" in layer:
        changes["allowed_tools"] = layer["allowed_tools"]
    if "blocked_tools" in layer:
        changes["blocked_tools"] = layer["blocked_tools"]
    return replace(base, **changes)


# ── parsing / validation ─────────────────────────────────────────────────────────────────────


def _parse_check(raw: Any, where: str, errors: list[str]) -> CheckSpec | None:
    if isinstance(raw, str):
        raw = {"check": raw}
    if not isinstance(raw, dict):
        errors.append(f"{where}: a check is a name or a mapping with `check:`")
        return None
    unknown = set(raw) - _CHECK_KEYS
    if unknown:
        errors.append(f"{where}: unknown key(s) {sorted(unknown)}")
    name = raw.get("check")
    kinds = check_kinds()
    if not isinstance(name, str) or name not in kinds:
        errors.append(f"{where}: unknown check {name!r} (known: {', '.join(sorted(kinds))})")
        return None
    kind = kinds[name]
    action = raw.get("action", kind.default_action)
    if action not in kind.allowed_actions:
        errors.append(
            f"{where}: check {name!r} action must be one of {list(kind.allowed_actions)},"
            f" not {action!r}"
        )
        return None
    timeout: float | None = None
    if "timeout_s" in raw:
        t = raw["timeout_s"]
        if (
            isinstance(t, bool)
            or not isinstance(t, (int, float))
            or not 0 < t <= MAX_CHECK_TIMEOUT_S
        ):
            errors.append(f"{where}: timeout_s must be a number in (0, {MAX_CHECK_TIMEOUT_S}]")
            return None
        timeout = float(t)
    elif kind.framework:
        timeout = DEFAULT_FRAMEWORK_TIMEOUT_S
    params: dict[str, Any] = {}
    if name == "length":
        mc = raw.get("max_chars")
        if isinstance(mc, bool) or not isinstance(mc, int) or mc <= 0:
            errors.append(f"{where}: `length` needs a positive integer max_chars")
            return None
        params["max_chars"] = mc
    elif "max_chars" in raw:
        errors.append(f"{where}: max_chars applies only to the `length` check")
    if name == "llm_guard":
        from examlops.guardrails.frameworks import validate_llm_guard_scanners

        scanners = raw.get("scanners")
        errs = validate_llm_guard_scanners(scanners)
        errors.extend(f"{where}: {e}" for e in errs)
        if errs:
            return None
        # JSON, so the spec stays hashable (it keys the built-check cache) and round-trips exactly.
        params["scanners"] = json.dumps(scanners, sort_keys=True)
        params["fail_fast"] = bool(raw.get("fail_fast", True))
    elif "scanners" in raw or "fail_fast" in raw:
        errors.append(f"{where}: scanners/fail_fast apply only to the llm_guard check")
    if "config" in raw:
        if name in _BUILTINS:
            errors.append(f"{where}: `config` applies only to a plugin check")
        elif not isinstance(raw["config"], dict):
            errors.append(f"{where}: `config` must be a mapping")
        else:
            try:
                params["config"] = json.dumps(raw["config"], sort_keys=True)
            except (TypeError, ValueError) as exc:
                errors.append(f"{where}: `config` is not plain data: {exc}")
    return CheckSpec(name, action, timeout, tuple(sorted(params.items())))


def _str_list(raw: Any, where: str, errors: list[str], cap: int) -> tuple[str, ...] | None:
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        errors.append(f"{where}: must be a list of non-empty strings")
        return None
    if len(raw) > cap:
        errors.append(f"{where}: at most {cap} entries")
        return None
    return tuple(x.strip() for x in raw)


def _parse_layer(raw: Any, where: str, errors: list[str], keys: frozenset[str]) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be a mapping")
        return {}
    unknown = set(raw) - keys
    if unknown:
        errors.append(f"{where}: unknown key(s) {sorted(unknown)}")
    out: dict[str, Any] = {}
    if "mode" in raw:
        if raw["mode"] not in MODES:
            errors.append(f"{where}.mode: must be one of {list(MODES)}")
        else:
            out["mode"] = raw["mode"]
    for direction in DIRECTIONS:
        if direction not in raw:
            continue
        items = raw[direction]
        if not isinstance(items, list):
            errors.append(f"{where}.{direction}: must be a list of checks")
            continue
        if len(items) > MAX_CHECKS_PER_DIRECTION:
            errors.append(f"{where}.{direction}: at most {MAX_CHECKS_PER_DIRECTION} checks")
            continue
        specs = [_parse_check(c, f"{where}.{direction}[{i}]", errors) for i, c in enumerate(items)]
        out[direction] = tuple(s for s in specs if s is not None)
    if "banned_topics" in raw:
        topics = _str_list(raw["banned_topics"], f"{where}.banned_topics", errors, MAX_TOPICS)
        if topics is not None:
            out["banned_topics"] = topics
    if "allowed_tools" in raw:
        if raw["allowed_tools"] is None:
            out["allowed_tools"] = None
        else:
            tools = _str_list(raw["allowed_tools"], f"{where}.allowed_tools", errors, 4096)
            if tools is not None:
                out["allowed_tools"] = frozenset(tools)
    if "blocked_tools" in raw:
        tools = _str_list(raw["blocked_tools"], f"{where}.blocked_tools", errors, 4096)
        if tools is not None:
            out["blocked_tools"] = frozenset(tools)
    return out


def parse_policy(raw: Any, source: str = "<inline>") -> GuardrailPolicy:
    """Validate a policy document; raise :class:`PolicyError` listing *every* problem."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise PolicyError([f"{source}: top level must be a mapping"])
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        errors.append(f"unknown top-level key(s) {sorted(unknown)}")
    version = raw.get("version", POLICY_VERSION)
    if version != POLICY_VERSION:
        errors.append(f"version: unsupported {version!r} (this build reads {POLICY_VERSION})")
    default = _parse_layer(raw.get("default"), "default", errors, _LAYER_KEYS)
    tenants: dict[str, dict[str, Any]] = {}
    raw_tenants = raw.get("tenants") or {}
    if not isinstance(raw_tenants, dict):
        errors.append("tenants: must be a mapping of tenant → policy")
    else:
        for t, layer in raw_tenants.items():
            if not isinstance(t, str) or not t:
                errors.append(f"tenants: invalid tenant name {t!r}")
                continue
            tenants[t] = _parse_layer(layer, f"tenants.{t}", errors, _LAYER_KEYS)
    routes: list[_Route] = []
    raw_routes = raw.get("routes") or []
    if not isinstance(raw_routes, list):
        errors.append("routes: must be a list")
    elif len(raw_routes) > MAX_ROUTES:
        errors.append(f"routes: at most {MAX_ROUTES} entries")
    else:
        for i, r in enumerate(raw_routes):
            where = f"routes[{i}]"
            if not isinstance(r, dict) or not isinstance(r.get("match"), str) or not r["match"]:
                errors.append(f"{where}: needs a non-empty `match` glob")
                continue
            tenant_glob = r.get("tenant", "*")
            if not isinstance(tenant_glob, str) or not tenant_glob:
                errors.append(f"{where}.tenant: must be a non-empty glob")
                continue
            layer = _parse_layer(
                {k: v for k, v in r.items() if k not in ("match", "tenant")},
                where,
                errors,
                _LAYER_KEYS,
            )
            routes.append(_Route(r["match"], tenant_glob, layer))
    # A `topics` check with nothing to match is a silent no-op; say so rather than ship it. A
    # route may inherit topics from the default or from a tenant it can match, so it is only an
    # error when no layer it could resolve through defines any.
    any_tenant_topics = any(v.get("banned_topics") for v in tenants.values())
    for i, r in enumerate(routes):
        for direction in DIRECTIONS:
            if "topics" in [c.check for c in r.layer.get(direction, ())] and not (
                r.layer.get("banned_topics") or default.get("banned_topics") or any_tenant_topics
            ):
                errors.append(f"routes[{i}].{direction}: `topics` check with no banned_topics")
    for label, layer in [("default", default), *((f"tenants.{t}", v) for t, v in tenants.items())]:
        for direction in DIRECTIONS:
            names = [c.check for c in layer.get(direction, ())]
            if (
                "topics" in names
                and not layer.get("banned_topics")
                and not default.get("banned_topics")
            ):
                errors.append(f"{label}.{direction}: `topics` check with no banned_topics")
    if errors:
        raise PolicyError(errors)
    return GuardrailPolicy(default, tenants, tuple(routes), source)


def load_policy_file(path: str | Path) -> GuardrailPolicy:
    import yaml

    p = Path(path)
    try:
        # Read at most one byte past the cap rather than trusting a stat taken before the read:
        # the file can grow between the two, and the cap is what bounds the parse.
        with p.open("rb") as fh:
            data = fh.read(MAX_POLICY_BYTES + 1)
        if len(data) > MAX_POLICY_BYTES:
            raise PolicyError([f"{p.name}: exceeds the {MAX_POLICY_BYTES}-byte cap"])
        raw = yaml.safe_load(data.decode("utf-8"))
    except PolicyError:
        raise
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        raise PolicyError([f"{p.name}: cannot read: {exc}"]) from exc
    return parse_policy(raw if raw is not None else {}, str(p))


def env_mode() -> str:
    """The deployment-wide mode, ``EXAMLOPS_GUARDRAIL_MODE`` (``monitor`` when unset/unknown).

    It is the base mode of every policy resolution: a layer that does not set ``mode:`` inherits
    it, so a policy file can tighten or loosen a route explicitly but never by omission.
    """
    raw = os.getenv("EXAMLOPS_GUARDRAIL_MODE", "").strip().lower()
    return raw if raw in MODES else "monitor"


def default_policy_path() -> Path | None:
    """``EXAMLOPS_GUARDRAIL_POLICY``, else ``<config dir>/guardrails.yaml`` when that exists."""
    if raw := os.getenv("EXAMLOPS_GUARDRAIL_POLICY", "").strip():
        return Path(raw).expanduser()
    from examlops.lifecycle.datadir import config_dir

    candidate = config_dir() / "guardrails.yaml"
    return candidate if candidate.is_file() else None


_file_cache: dict[str, tuple[tuple[int, int], GuardrailPolicy | PolicyError]] = {}
_file_cache_lock = threading.Lock()
_invalid_reported: set[tuple[str, tuple[int, int]]] = set()


def cached_policy(path: Path) -> GuardrailPolicy:
    """The policy at ``path``, re-read only when its (mtime, size) changes. Raises PolicyError."""
    key = str(path)
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError as exc:
        raise PolicyError([f"{path.name}: cannot read: {exc}"]) from exc
    with _file_cache_lock:
        hit = _file_cache.get(key)
    if hit is None or hit[0] != stamp:
        try:
            value: GuardrailPolicy | PolicyError = load_policy_file(path)
        except PolicyError as exc:
            value = exc
        with _file_cache_lock:
            _file_cache[key] = (stamp, value)
        # A changed file may change what a check is built with; rebuild on next use.
        _built.clear()
        hit = (stamp, value)
    value = hit[1]
    if isinstance(value, PolicyError):
        _report_invalid(key, hit[0], value)
        raise value
    return value


def _report_invalid(key: str, stamp: tuple[int, int], err: PolicyError) -> None:
    with _file_cache_lock:
        if (key, stamp) in _invalid_reported:
            return
        _invalid_reported.add((key, stamp))
    log.error(
        "guardrail policy %s is invalid — falling back to the built-in guardrail: %s", key, err
    )
    try:
        from examlops.data.audit import audit_best_effort

        audit_best_effort(
            "exa-guardrails", None, "guardrail_policy_invalid", key, {"errors": err.errors[:20]}
        )
    except Exception:  # noqa: BLE001 - reporting must never take the boundary down
        pass


def clear_caches() -> None:
    """Forget cached policy files, built checks and plugin discovery (tests, hot reload)."""
    global _plugins
    with _file_cache_lock:
        _file_cache.clear()
        _invalid_reported.clear()
    with _registry_lock:
        _plugins = None
    _built.clear()


# ── the guardrail ────────────────────────────────────────────────────────────────────────────

_executor_lock = threading.Lock()
_executor: concurrent.futures.ThreadPoolExecutor | None = None
_built: dict[tuple[CheckSpec, tuple[str, ...]], Check | tuple[Exception, float]] = {}
_MAX_BUILT = 512
#: A check that could not be built is retried after this long. Caching the failure forever would
#: turn one transient error (a model download, a plugin's backend briefly down) into a permanent
#: block in enforce until the process restarts; retrying on every call would rebuild per request.
_BUILD_RETRY_S = 30.0


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            workers = int(os.getenv("EXAMLOPS_GUARDRAIL_WORKERS", "4") or 4)
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, min(workers, 32)), thread_name_prefix="guardrail-check"
            )
        return _executor


class CheckUnavailable(RuntimeError):
    """A policy names a check this environment cannot construct (extra missing, bad plugin)."""


def _build(spec: CheckSpec, policy: ResolvedPolicy) -> Check:
    # Keyed on the spec and the topics it may read — a framework scanner is expensive to build
    # (LLM-Guard loads a model), so it is built once per configuration, never per request.
    key = (spec, policy.banned_topics)
    cached = _built.get(key)
    if isinstance(cached, tuple) and time.monotonic() - cached[1] >= _BUILD_RETRY_S:
        cached = None
    if cached is None:
        kind = check_kinds().get(spec.check)
        try:
            if kind is None:
                raise CheckUnavailable(f"no check named {spec.check!r}")
            if kind.availability is not None and (reason := kind.availability()):
                raise CheckUnavailable(reason)
            cached = kind.factory(spec, policy)
        except Exception as exc:  # noqa: BLE001 - recorded, then handled by the caller's mode
            err = exc if isinstance(exc, CheckUnavailable) else CheckUnavailable(str(exc))
            cached = (err, time.monotonic())
        if len(_built) >= _MAX_BUILT:
            _built.clear()
        _built[key] = cached
    if isinstance(cached, tuple):
        raise cached[0]
    return cached


def _run_check(check: Check, spec: CheckSpec, text: str, direction: str, ctx: dict) -> CheckOutcome:
    if spec.timeout_s is None:
        return check.run(text, direction, ctx)
    fut = _pool().submit(check.run, text, direction, ctx)
    return fut.result(timeout=spec.timeout_s)


@dataclass
class PolicyGuardrail:
    """A :class:`Guardrail` whose checks, mode and tool lists come from a declarative policy."""

    policy: GuardrailPolicy | None = None
    tenant: str = "default"
    #: Loaded from here (hot-reloaded) when ``policy`` is not given.
    path: Path | None = None
    #: Force a mode for every resolution (the ``exa guardrails test --mode`` override).
    mode_override: str | None = None
    #: Mode used when the policy file is invalid and the built-in guardrail takes over.
    fallback_mode: str = "monitor"

    # -- resolution -------------------------------------------------------------------------
    def current(self) -> GuardrailPolicy | None:
        if self.policy is not None:
            return self.policy
        if self.path is None:
            return None
        try:
            return cached_policy(self.path)
        except PolicyError:
            return None

    def resolve(self, ctx: dict | None = None) -> ResolvedPolicy | None:
        pol = self.current()
        if pol is None:
            return None
        ctx = ctx or {}
        tenant = str(ctx.get("tenant") or self.tenant)
        route = ctx.get("route") or ctx.get("model")
        resolved = pol.resolve(tenant, str(route) if route else None, base_mode=self.fallback_mode)
        if self.mode_override:
            resolved = replace(resolved, mode=self.mode_override)
        return resolved

    def _fallback(self, ctx: dict | None):
        from examlops.guardrails import DefaultGuardrail

        tenant = str((ctx or {}).get("tenant") or self.tenant)
        return DefaultGuardrail(mode=self.mode_override or self.fallback_mode, tenant=tenant)

    # -- Guardrail protocol ------------------------------------------------------------------
    def check_input(self, text: str, ctx: dict | None = None):
        return self._check("input", text, ctx)

    def check_output(self, text: str, ctx: dict | None = None):
        return self._check("output", text, ctx)

    def check_tool_call(self, tool: str, ctx: dict | None = None) -> bool:
        resolved = self.resolve(ctx)
        if resolved is None:
            return self._fallback(ctx).check_tool_call(tool, ctx)
        if resolved.mode == "off":
            return True
        denied = tool in resolved.blocked_tools or (
            resolved.allowed_tools is not None and tool not in resolved.allowed_tools
        )
        if not denied:
            return True
        # Only an enforced denial is a block. In monitor the call proceeds, so recording it as
        # ``block`` would write a ``guardrail_block`` audit event for something that happened.
        enforce = resolved.mode == "enforce"
        self._recorder(resolved, ctx)._record("tool", "block" if enforce else "allow", tool)
        return not enforce

    # -- engine ------------------------------------------------------------------------------
    def _recorder(self, resolved: ResolvedPolicy, ctx: dict | None):
        from examlops.guardrails import DefaultGuardrail

        tenant = str((ctx or {}).get("tenant") or self.tenant)
        return DefaultGuardrail(mode=resolved.mode, tenant=tenant)

    def _check(self, direction: str, text: str, ctx: dict | None):
        from examlops.guardrails import GuardResult

        resolved = self.resolve(ctx)
        if resolved is None:
            fb = self._fallback(ctx)
            return fb.check_input(text, ctx) if direction == "input" else fb.check_output(text, ctx)
        if resolved.mode == "off":
            return GuardResult("allow", text)
        ctx = dict(ctx or {})
        enforce = resolved.mode == "enforce"
        rec = self._recorder(resolved, ctx)
        findings: list[str] = []
        blocking: list[str] = []
        current = text
        redacted = False
        for spec in resolved.checks(direction):
            try:
                check = _build(spec, resolved)
                outcome = _run_check(check, spec, current, direction, ctx)
            except CheckUnavailable as exc:
                tag = f"check-unavailable:{spec.check}"
                log.warning("guardrail check %s unavailable: %s", spec.check, exc)
                findings.append(tag)
                if enforce:
                    blocking.append(tag)
                    break
                continue
            except concurrent.futures.TimeoutError:
                tag = f"scanner-timeout:{spec.check}"
                findings.append(tag)
                if enforce:
                    blocking.append(tag)
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - fail closed in enforce (R6)
                log.warning("guardrail check %s failed: %s", spec.check, exc)
                tag = f"scanner-error:{spec.check}"
                findings.append(tag)
                if enforce:
                    blocking.append(tag)
                    break
                continue
            if not outcome.findings:
                continue
            findings.extend(f for f in outcome.findings if f not in findings)
            if outcome.hard_block or spec.action == "block":
                blocking.extend(outcome.findings)
                if enforce:
                    break
            elif spec.action == "redact":
                if outcome.text is not None and outcome.text != current:
                    current = outcome.text
                    redacted = True
                else:
                    # The check found something and offered no redaction (e.g. an LLM-Guard
                    # classifier such as PromptInjection returns the text unchanged). Passing it
                    # on as "redacted" would let exactly what was detected through; fail closed.
                    tag = f"unredactable:{spec.check}"
                    findings.append(tag)
                    blocking.append(tag)
                    if enforce:
                        break
            # "flag": recorded, text untouched
        if not findings:
            return GuardResult("allow", text)
        rule = ",".join(findings)
        if not enforce:
            rec._record(direction, "allow", rule)
            return GuardResult("allow", text, findings, "monitor: not blocked")
        if blocking:
            rec._record(direction, "block", ",".join(blocking))
            return GuardResult("block", "", findings, f"policy blocked: {', '.join(blocking)}")
        if redacted:
            rec._record(direction, "redact", rule)
            return GuardResult("redact", current, findings, "policy redacted")
        rec._record(direction, "allow", rule)
        return GuardResult("allow", text, findings, "flagged")


def policy_guardrail(tenant: str = "default", *, fallback_mode: str = "monitor"):
    """A :class:`PolicyGuardrail` when a policy file is configured, else ``None``."""
    path = default_policy_path()
    if path is None:
        return None
    return PolicyGuardrail(tenant=tenant, path=path, fallback_mode=fallback_mode)


def validate_policy_file(path: str | Path) -> dict[str, Any]:
    """Validation report for ``exa guardrails policy validate``: errors + check availability."""
    report: dict[str, Any] = {"path": str(path), "valid": False, "errors": [], "unavailable": []}
    try:
        pol = load_policy_file(path)
    except PolicyError as exc:
        report["errors"] = exc.errors
        return report
    report["valid"] = True
    names: set[str] = set()
    layers = [pol.default, *pol.tenants.values(), *(r.layer for r in pol.routes)]
    for layer in layers:
        for direction in DIRECTIONS:
            names.update(c.check for c in layer.get(direction, ()))
    avail = {row["check"]: row for row in list_checks()}
    report["unavailable"] = [
        {"check": n, "reason": avail[n]["reason"]}
        for n in sorted(names)
        if n in avail and not avail[n]["available"]
    ]
    report["tenants"] = sorted(pol.tenants)
    report["routes"] = [{"match": r.match, "tenant": r.tenant} for r in pol.routes]
    return report


def tool_call_gate(tool: str, context: dict[str, Any] | None = None) -> str | None:
    """Agent tool-call check against the guardrail policy (ADR 0026 clause 4).

    Returns an error message when the configured policy denies ``tool`` in ``enforce`` mode, else
    ``None``. Inert (``None``) when no policy file is configured or ``EXAMLOPS_GUARDRAIL_MODE=off``
    — the tool lists live in the policy, so without one there is nothing to enforce. The tenant is
    ``context["tenant"]``, else ``EXAMLOPS_TENANT``, else ``default``. An *invalid* policy file
    denies: the operator wrote a tool list, and a typo must not silently grant every tool.
    """
    if os.getenv("EXAMLOPS_GUARDRAIL_MODE", "").strip().lower() == "off":
        return None
    path = default_policy_path()
    if path is None:
        return None
    ctx = dict(context or {})
    tenant = str(ctx.get("tenant") or os.getenv("EXAMLOPS_TENANT") or "default")
    try:
        cached_policy(path)
    except PolicyError:
        return f"guardrail policy {path.name} is invalid; refusing agent tool call ({tool})"
    guard = PolicyGuardrail(tenant=tenant, path=path, fallback_mode=env_mode())
    if guard.check_tool_call(tool, {"tenant": tenant}):
        return None
    return f"guardrail policy denies tool {tool!r} for tenant {tenant!r}"
