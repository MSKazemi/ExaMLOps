"""Typed, versioned tool grants and the pure decision function (ADR 0145 d2, d3).

A **grant set** is the list of :class:`ToolGrant` stored for one *subject* - an agent version id,
an agent name, or a workload-identity subject. This module holds no state and does no I/O:
:func:`decide_tool_call` is a pure function of ``(grant_set, call)``.

**The default, stated exactly**

* A caller for whom *no* grant set exists is **not brokered**: the decision is ``allow`` (reason
  ``no_grant_set``), i.e. exactly what happens today. Turning the broker on changes nothing for an
  agent nobody has written grants for.
* A caller for whom a grant set exists is **default-deny**: a tool with no matching grant (neither
  the tool's own nor a ``*`` grant) is denied. An exact-tool grant overrides a ``*`` grant.
* Removing the *last* grant of a subject therefore returns it to the un-brokered default. To keep a
  subject locked out, store an explicit ``*`` grant with ``effect: deny``.

Where several subjects apply to one caller, the most specific set wins **whole** (version id, then
agent name, then workload subject); sets are never merged, so a version-pinned set can be narrower
or wider than its agent's, and the reader never has to compute a union.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "GRANT_SCHEMA_VERSION",
    "GrantError",
    "GrantSet",
    "ToolCall",
    "ToolCaller",
    "ToolDecision",
    "ToolGrant",
    "check_schema",
    "decide_tool_call",
    "parse_grant",
    "resolve_subjects",
    "tool_visible",
    "validate_args",
]

GRANT_SCHEMA_VERSION = 1
TIER_RANK = {"read": 0, "A": 1, "B": 2, "C": 3}
EFFECTS = ("allow", "deny")
ANY_TOOL = "*"

_GRANT_KEYS = {
    "schema_version",
    "effect",
    "tier_ceiling",
    "needs_approval",
    "max_calls_per_minute",
    "max_calls_per_session",
    "arg_schema",
    "credentials",
    "egress",
}
_EGRESS_KEYS = {"url_args", "allowed_hosts"}
_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "const",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "items",
    "minItems",
    "maxItems",
}
_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}
_MAX_PATTERN = 256
#: A ``pattern`` is only ever matched against text at most this long - a bound on regex cost, not
#: a limit on what an argument may be (a longer string simply fails the pattern).
_MAX_MATCH_LEN = 4096
_NAME = re.compile(r"\*|[A-Za-z0-9_.:@/+-]{1,200}")


class GrantError(ValueError):
    """A grant document is invalid; ``problems`` names each defect."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


# ── value types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolGrant:
    tool: str
    effect: str = "allow"
    tier_ceiling: str | None = None
    needs_approval: bool = False
    max_calls_per_minute: int | None = None
    max_calls_per_session: int | None = None
    arg_schema: Mapping[str, Any] | None = None
    #: ``{tool parameter name: secret path}`` - injected by the broker, never by the agent.
    credentials: Mapping[str, str] = field(default_factory=dict)
    #: ``{"url_args": [...], "allowed_hosts": [...]}`` for tools that take a URL.
    egress: Mapping[str, Any] | None = None

    def to_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"schema_version": GRANT_SCHEMA_VERSION, "effect": self.effect}
        if self.tier_ceiling:
            doc["tier_ceiling"] = self.tier_ceiling
        if self.needs_approval:
            doc["needs_approval"] = True
        if self.max_calls_per_minute is not None:
            doc["max_calls_per_minute"] = self.max_calls_per_minute
        if self.max_calls_per_session is not None:
            doc["max_calls_per_session"] = self.max_calls_per_session
        if self.arg_schema is not None:
            doc["arg_schema"] = dict(self.arg_schema)
        if self.credentials:
            doc["credentials"] = dict(self.credentials)
        if self.egress:
            doc["egress"] = dict(self.egress)
        return doc


@dataclass(frozen=True)
class GrantSet:
    """The grants of one subject, keyed by tool name (or ``*``)."""

    subject: str
    grants: Mapping[str, ToolGrant]


@dataclass(frozen=True)
class ToolCaller:
    """Who is calling. ``agent`` is required; the rest sharpen which grant set applies."""

    agent: str
    version_id: str | None = None
    subject: str | None = None  # workload identity (ADR 0125)
    on_behalf_of: str | None = None
    session: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class ToolCall:
    caller: ToolCaller
    tool: str
    args: Mapping[str, Any]
    tier: str = "read"


@dataclass(frozen=True)
class ToolDecision:
    effect: str  # allow | deny | require_approval
    code: str
    reason: str
    subject: str | None = None
    grant: ToolGrant | None = None
    problems: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


# ── the schema subset ─────────────────────────────────────────────────────────


def check_schema(schema: Any, path: str = "arg_schema") -> list[str]:
    """Problems with ``schema`` as a member of the supported JSON-Schema subset (empty = fine)."""
    if not isinstance(schema, dict):
        return [f"{path}: must be an object"]
    out: list[str] = []
    for k in schema:
        if k not in _SCHEMA_KEYS:
            out.append(f"{path}.{k}: unsupported keyword (supported: {sorted(_SCHEMA_KEYS)})")
    t = schema.get("type")
    if t is not None and t not in _TYPES:
        out.append(f"{path}.type: must be one of {sorted(_TYPES)}")
    props = schema.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            out.append(f"{path}.properties: must be an object")
        else:
            for name, sub in props.items():
                out += check_schema(sub, f"{path}.properties.{name}")
    req = schema.get("required")
    if req is not None and not (isinstance(req, list) and all(isinstance(r, str) for r in req)):
        out.append(f"{path}.required: must be a list of strings")
    ap = schema.get("additionalProperties")
    if isinstance(ap, dict):
        out += check_schema(ap, f"{path}.additionalProperties")
    elif ap is not None and not isinstance(ap, bool):
        out.append(f"{path}.additionalProperties: must be a boolean or a schema")
    if "enum" in schema and not isinstance(schema["enum"], list):
        out.append(f"{path}.enum: must be a list")
    pat = schema.get("pattern")
    if pat is not None:
        if not isinstance(pat, str) or len(pat) > _MAX_PATTERN:
            out.append(f"{path}.pattern: must be a string of at most {_MAX_PATTERN} characters")
        else:
            try:
                re.compile(pat)
            except re.error as exc:
                out.append(f"{path}.pattern: not a valid regular expression ({exc})")
    for k in ("minLength", "maxLength", "minItems", "maxItems"):
        if k in schema and not (isinstance(schema[k], int) and not isinstance(schema[k], bool)):
            out.append(f"{path}.{k}: must be an integer")
    for k in ("minimum", "maximum"):
        if k in schema and (isinstance(schema[k], bool) or not isinstance(schema[k], int | float)):
            out.append(f"{path}.{k}: must be a number")
    if "items" in schema:
        out += check_schema(schema["items"], f"{path}.items")
    return out


def _type_ok(value: Any, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    return value is None  # "null"


def validate_args(schema: Mapping[str, Any], value: Any, path: str = "args") -> list[str]:
    """Violations of ``schema`` by ``value`` (empty = valid). Values are never echoed back.

    The problem strings name the *location* and the rule, not the offending value: they end up in
    audit rows and in the answer to an agent, and an argument may be a credential.
    """
    out: list[str] = []
    t = schema.get("type")
    if t is not None and not _type_ok(value, t):
        return [f"{path}: must be of type {t}"]
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: does not equal the required constant")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: is not one of the allowed values")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and (
            len(value) > _MAX_MATCH_LEN or re.fullmatch(schema["pattern"], value) is None
        ):
            out.append(f"{path}: does not match the allowed pattern")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: above the maximum {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                out += validate_args(schema["items"], item, f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                out.append(f"{path}.{name}: required")
        ap = schema.get("additionalProperties", True)
        for name, v in value.items():
            if name in props:
                out += validate_args(props[name], v, f"{path}.{name}")
            elif ap is False:
                out.append(f"{path}.{name}: not an allowed argument")
            elif isinstance(ap, dict):
                out += validate_args(ap, v, f"{path}.{name}")
    return out


# ── parsing ───────────────────────────────────────────────────────────────────


def parse_grant(tool: str, doc: Mapping[str, Any]) -> ToolGrant:
    """Validate ``doc`` (the stored/typed form) into a :class:`ToolGrant`, or raise."""
    problems: list[str] = []
    if not isinstance(tool, str) or not _NAME.fullmatch(tool):
        problems.append("tool: a tool name or '*'")
    if not isinstance(doc, Mapping):
        raise GrantError(["grant: must be an object"])
    for k in doc:
        if k not in _GRANT_KEYS:
            problems.append(f"{k}: unknown field")
    ver = doc.get("schema_version", GRANT_SCHEMA_VERSION)
    if ver != GRANT_SCHEMA_VERSION:
        problems.append(f"schema_version: {ver!r} is not supported (this build reads 1)")
    effect = doc.get("effect", "allow")
    if effect not in EFFECTS:
        problems.append(f"effect: must be one of {list(EFFECTS)}")
    ceiling = doc.get("tier_ceiling")
    if ceiling is not None and ceiling not in TIER_RANK:
        problems.append(f"tier_ceiling: must be one of {list(TIER_RANK)}")
    na = doc.get("needs_approval", False)
    if not isinstance(na, bool):
        problems.append("needs_approval: must be a boolean")
    limits: dict[str, int | None] = {}
    for k in ("max_calls_per_minute", "max_calls_per_session"):
        v = doc.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            problems.append(f"{k}: must be an integer >= 1")
            v = None
        limits[k] = v
    schema = doc.get("arg_schema")
    if schema is not None:
        problems += check_schema(schema)
    creds = doc.get("credentials") or {}
    if not isinstance(creds, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) and k and v for k, v in creds.items()
    ):
        problems.append("credentials: must map parameter name -> secret path (both non-empty)")
        creds = {}
    egress = doc.get("egress")
    if egress is not None:
        if not isinstance(egress, Mapping) or set(egress) - _EGRESS_KEYS:
            problems.append(f"egress: an object with only {sorted(_EGRESS_KEYS)}")
        else:
            ua, ah = egress.get("url_args"), egress.get("allowed_hosts")
            if not (isinstance(ua, list) and ua and all(isinstance(x, str) for x in ua)):
                problems.append("egress.url_args: a non-empty list of argument names")
            if not (isinstance(ah, list) and ah and all(isinstance(x, str) for x in ah)):
                problems.append("egress.allowed_hosts: a non-empty list of host names")
    if problems:
        raise GrantError(problems)
    return ToolGrant(
        tool=tool,
        effect=effect,
        tier_ceiling=ceiling,
        needs_approval=na,
        max_calls_per_minute=limits["max_calls_per_minute"],
        max_calls_per_session=limits["max_calls_per_session"],
        arg_schema=schema,
        credentials=dict(creds),
        egress=dict(egress) if egress else None,
    )


def resolve_subjects(caller: ToolCaller) -> list[str]:
    """Subjects to look up, most specific first: version id, agent name, workload subject."""
    out: list[str] = []
    for s in (caller.version_id, caller.agent, caller.subject):
        if s and s not in out:
            out.append(s)
    return out


# ── the decision ──────────────────────────────────────────────────────────────


def host_allowed(host: str, patterns: Sequence[str]) -> bool:
    """Exact host, or ``*.suffix`` (matches subdomains, not the bare suffix). Case-insensitive."""
    h = host.strip(".").lower()
    for p in patterns:
        p = p.strip().lower()
        if p.startswith("*."):
            if h.endswith(p[1:]) and len(h) > len(p) - 1:
                return True
        elif h == p:
            return True
    return False


def _find(gs: GrantSet, tool: str) -> ToolGrant | None:
    return gs.grants.get(tool) or gs.grants.get(ANY_TOOL)


def decide_tool_call(grant_set: GrantSet | None, call: ToolCall) -> ToolDecision:
    """Allow, deny or ``require_approval`` one tool call. Pure. See the module docstring.

    Rate limits and DNS-level egress checks are stateful/impure and are applied by the broker
    after this returns ``allow``; the limits it must apply are on ``decision.grant``.
    """
    if grant_set is None:
        return ToolDecision("allow", "no_grant_set", "no grant set for this caller (not brokered)")
    g = _find(grant_set, call.tool)
    sub = grant_set.subject
    if g is None:
        return ToolDecision(
            "deny", "no_grant", f"{sub} holds grants but none covers {call.tool}", sub
        )
    if g.effect == "deny":
        return ToolDecision("deny", "denied_by_grant", f"{call.tool} is denied to {sub}", sub, g)
    if g.tier_ceiling and TIER_RANK.get(call.tier, 3) > TIER_RANK[g.tier_ceiling]:
        return ToolDecision(
            "deny",
            "tier_exceeds_ceiling",
            f"{call.tool} is tier {call.tier}; the grant's ceiling is {g.tier_ceiling}",
            sub,
            g,
        )
    smuggled = sorted(set(g.credentials) & set(call.args))
    if smuggled:
        return ToolDecision(
            "deny",
            "credential_in_args",
            f"argument(s) {smuggled} are injected by the broker and may not be supplied",
            sub,
            g,
        )
    if g.arg_schema is not None:
        problems = validate_args(g.arg_schema, dict(call.args))
        if problems:
            return ToolDecision(
                "deny",
                "argument_violation",
                "arguments violate the grant's arg_schema",
                sub,
                g,
                tuple(problems),
            )
    if g.egress:
        bad = _egress_problems(g.egress, call.args)
        if bad:
            return ToolDecision(
                "deny",
                "egress_denied",
                "a URL argument is outside the allow-list",
                sub,
                g,
                tuple(bad),
            )
    if g.needs_approval:
        return ToolDecision(
            "require_approval", "needs_approval", f"{call.tool} needs human approval", sub, g
        )
    return ToolDecision("allow", "granted", f"granted to {sub}", sub, g)


def tool_visible(grant_set: GrantSet | None, tool: str, tier: str = "read") -> bool:
    """Could this caller ever call ``tool``? The ``tools/list`` filter (ADR 0145 d2).

    Ignores arguments and approval on purpose: a tool that needs approval, or whose arguments are
    constrained, is still *visible*; one that is denied, ungranted or above the tier ceiling is not.
    """
    if grant_set is None:
        return True
    g = _find(grant_set, tool)
    if g is None or g.effect == "deny":
        return False
    return not (g.tier_ceiling and TIER_RANK.get(tier, 3) > TIER_RANK[g.tier_ceiling])


def url_host(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def _egress_problems(egress: Mapping[str, Any], args: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for name in egress.get("url_args", []):
        if name not in args:
            continue
        host = url_host(args[name])
        if not host:
            out.append(f"args.{name}: not a URL with a host")
        elif not host_allowed(host, egress.get("allowed_hosts", [])):
            out.append(f"args.{name}: host is not in the allow-list")
    return out
