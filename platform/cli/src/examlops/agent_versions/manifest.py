"""The ``AgentVersion`` manifest (ADR 0146 decision 1): typed, canonical, content-addressed.

An agent's behaviour is a tuple - code, prompts, models, tools, policy - and a version is only
useful if it names every member **by something that cannot move**: an image digest, a prompt
version number, a model version, a tool-schema hash. This module validates that, and hashes it.

Validation is strict on purpose. Unknown fields are refused (a typo is not a free comment), a
floating reference is refused (``label``/``alias`` on a prompt, a tag-only image, a ``pin``
binding without a version), and a missing required component is named. ``follow`` model bindings
are the one deliberate floating reference (decision 2): they carry an alias and *no* version, and
the version they resolve to is not part of the identity.

Everything here is pure - no database, no network.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

__all__ = [
    "ALIASES",
    "AUTONOMY_LEVELS",
    "SCHEMA_VERSION",
    "AgentManifestError",
    "canonical_json",
    "diff_manifests",
    "mcp_tool_manifest",
    "normalize",
    "tool_manifest_hash",
    "version_id_of",
]

SCHEMA_VERSION = 1
#: The alias machinery is the model registry's (ADR 0146 decision 1), spelled the same way.
ALIASES = ("Staging", "Canary", "Production")
#: The SAE-style autonomy scale ADR 0113 adopts (arXiv:2602.04261).
AUTONOMY_LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
_SUITE = re.compile(r"^[A-Za-z0-9._-]+@[0-9]+$")
_MODEL_ALIAS = {a.lower(): a for a in ALIASES}

_REQUIRED = ("schema_version", "agent", "code", "prompts", "models", "tools", "policy")
_OPTIONAL = ("memory", "state", "eval", "sandbox", "budgets", "guardrails")
_ID_KEYS = ("version_id",)


class AgentManifestError(ValueError):
    """The manifest is not a valid ``AgentVersion``; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def canonical_json(doc: Any) -> str:
    """Byte-stable JSON: sorted keys, no whitespace, UTF-8 kept, NaN/Infinity refused."""
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def tool_manifest_hash(tools: list[dict[str, Any]]) -> str:
    """The hash of the tool set: names and schema hashes, order-independent."""
    rows = sorted(({"name": t["name"], "schema_hash": t["schema_hash"]} for t in tools), key=str)
    return _sha(canonical_json(rows))


def version_id_of(manifest: dict[str, Any]) -> str:
    """``av-sha256:<hex>`` over the normalised manifest (``version_id`` itself excluded)."""
    body = {k: v for k, v in manifest.items() if k not in _ID_KEYS}
    return "av-" + _sha(canonical_json(body))


def mcp_tool_manifest(names: list[str]) -> list[dict[str, str]]:
    """``[{name, schema_hash}]`` for MCP tools in the platform registry, by name.

    The hash covers the tool's name, its first-paragraph description and its parameter list, so
    a tool whose contract changes is a different pin. An unknown name raises ``LookupError``.
    """
    import inspect

    from examlops.mcp.tools import REGISTRY

    by_name = {spec.name: spec for spec in REGISTRY}
    out: list[dict[str, str]] = []
    for name in sorted(set(names)):
        spec = by_name.get(name)
        if spec is None:
            raise LookupError(f"no MCP tool named {name!r} in the registry")
        sig = [
            [p.name, str(p.annotation), repr(p.default)]
            for p in inspect.signature(spec.fn).parameters.values()
        ]
        out.append(
            {
                "name": name,
                "schema_hash": _sha(
                    canonical_json({"d": spec.description, "p": sig, "m": spec.mutating})
                ),
            }
        )
    return out


# -- validation --------------------------------------------------------------------------------


def _unknown(where: str, obj: dict[str, Any], allowed: set[str], problems: list[str]) -> None:
    for key in sorted(set(obj) - allowed):
        if key in ("label", "alias", "tag", "latest"):
            problems.append(
                f"{where}.{key}: a floating reference is refused - pin an immutable version"
            )
        else:
            problems.append(f"{where}.{key}: unknown field")


def _str(
    where: str, v: Any, problems: list[str], *, pattern: re.Pattern[str] | None = None
) -> bool:
    if not isinstance(v, str) or not v.strip():
        problems.append(f"{where}: must be a non-empty string")
        return False
    if pattern is not None and not pattern.match(v):
        problems.append(f"{where}: {v!r} does not match the required form")
        return False
    return True


def _obj(where: str, v: Any, problems: list[str]) -> dict[str, Any] | None:
    if not isinstance(v, dict):
        problems.append(f"{where}: must be an object")
        return None
    return v


def _int(where: str, v: Any, problems: list[str], *, minimum: int = 1) -> bool:
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        problems.append(f"{where}: must be an integer >= {minimum}")
        return False
    return True


def _num(where: str, v: Any, problems: list[str], *, lo: float = 0.0, hi: float = math.inf) -> None:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if not ok or not lo <= v <= hi:
        problems.append(f"{where}: must be a finite number in [{lo}, {hi}]")


def _check_code(v: Any, problems: list[str]) -> None:
    o = _obj("code", v, problems)
    if o is None:
        return
    _unknown("code", o, {"image", "entrypoint", "framework"}, problems)
    if _str("code.image", o.get("image"), problems):
        if not _IMAGE_DIGEST.search(o["image"]):
            problems.append("code.image: must be pinned by digest (…@sha256:<64 hex>), not by tag")
    _str("code.entrypoint", o.get("entrypoint"), problems)
    if "framework" in o:
        _str("code.framework", o["framework"], problems)


def _check_prompts(v: Any, problems: list[str]) -> None:
    if not isinstance(v, list) or not v:
        problems.append("prompts: must be a non-empty list of {name, version}")
        return
    seen: set[str] = set()
    for i, p in enumerate(v):
        w = f"prompts[{i}]"
        o = _obj(w, p, problems)
        if o is None:
            continue
        _unknown(w, o, {"name", "version", "role"}, problems)
        if _str(f"{w}.name", o.get("name"), problems):
            if o["name"] in seen:
                problems.append(f"{w}.name: {o['name']!r} listed twice")
            seen.add(o["name"])
        if "version" not in o:
            problems.append(f"{w}.version: required (a prompt is pinned by version number)")
        else:
            _int(f"{w}.version", o["version"], problems)
        if "role" in o:
            _str(f"{w}.role", o["role"], problems)


def _check_models(v: Any, problems: list[str]) -> None:
    if not isinstance(v, list) or not v:
        problems.append("models: must be a non-empty list")
        return
    roles: set[str] = set()
    for i, m in enumerate(v):
        w = f"models[{i}]"
        o = _obj(w, m, problems)
        if o is None:
            continue
        _unknown(w, o, {"role", "servable", "binding", "version", "alias"}, problems)
        if _str(f"{w}.role", o.get("role"), problems):
            if o["role"] in roles:
                problems.append(f"{w}.role: {o['role']!r} listed twice")
            roles.add(o["role"])
        _str(f"{w}.servable", o.get("servable"), problems)
        binding = o.get("binding")
        if binding == "pin":
            if "alias" in o:
                problems.append(f"{w}: a pin binding may not carry an alias")
            if o.get("version") in (None, ""):
                problems.append(f"{w}.version: a pin binding requires a version")
            elif not isinstance(o["version"], (str, int)) or isinstance(o["version"], bool):
                problems.append(f"{w}.version: must be a string or integer")
        elif binding == "follow":
            if "version" in o:
                problems.append(f"{w}: a follow binding may not carry a version")
            if str(o.get("alias", "")).lower() not in _MODEL_ALIAS:
                problems.append(f"{w}.alias: a follow binding requires one of {', '.join(ALIASES)}")
        else:
            problems.append(f"{w}.binding: must be 'pin' or 'follow'")


def _check_tools(v: Any, problems: list[str]) -> None:
    o = _obj("tools", v, problems)
    if o is None:
        return
    _unknown("tools", o, {"tools", "grants", "manifest_hash"}, problems)
    tools = o.get("tools")
    if not isinstance(tools, list):
        problems.append("tools.tools: required list of {name, schema_hash} (may be empty)")
        tools = []
    seen: set[str] = set()
    for i, t in enumerate(tools):
        w = f"tools.tools[{i}]"
        to = _obj(w, t, problems)
        if to is None:
            continue
        _unknown(w, to, {"name", "schema_hash"}, problems)
        if _str(f"{w}.name", to.get("name"), problems):
            if to["name"] in seen:
                problems.append(f"{w}.name: {to['name']!r} listed twice")
            seen.add(to["name"])
        if not _str(f"{w}.schema_hash", to.get("schema_hash"), problems, pattern=_SHA):
            continue
    grants = o.get("grants", [])
    if not isinstance(grants, list) or not all(isinstance(g, str) and g for g in grants):
        problems.append("tools.grants: must be a list of non-empty strings")


def _check_policy(v: Any, problems: list[str]) -> None:
    o = _obj("policy", v, problems)
    if o is None:
        return
    _unknown("policy", o, {"contract", "autonomy", "multitask_strategy"}, problems)
    if o.get("autonomy") not in AUTONOMY_LEVELS:
        problems.append(f"policy.autonomy: required, one of {', '.join(AUTONOMY_LEVELS)}")
    for k in ("contract", "multitask_strategy"):
        if k in o:
            _str(f"policy.{k}", o[k], problems)


def _check_optional(doc: dict[str, Any], problems: list[str]) -> None:
    if "memory" in doc and (o := _obj("memory", doc["memory"], problems)) is not None:
        _unknown("memory", o, {"scope", "store"}, problems)
        for k in ("scope", "store"):
            if k not in o:
                problems.append(f"memory.{k}: required when memory is declared")
            else:
                _str(f"memory.{k}", o[k], problems)
    if "state" in doc and (o := _obj("state", doc["state"], problems)) is not None:
        _unknown("state", o, {"schema_version", "schema_hash"}, problems)
        _int("state.schema_version", o.get("schema_version"), problems, minimum=0)
        _str("state.schema_hash", o.get("schema_hash"), problems, pattern=_SHA)
    if "eval" in doc and (o := _obj("eval", doc["eval"], problems)) is not None:
        _unknown("eval", o, {"suites", "non_inferiority_margin"}, problems)
        suites = o.get("suites")
        if not isinstance(suites, list) or not suites:
            problems.append("eval.suites: must be a non-empty list of name@version")
        else:
            for i, s in enumerate(suites):
                _str(f"eval.suites[{i}]", s, problems, pattern=_SUITE)
        if "non_inferiority_margin" in o:
            _num("eval.non_inferiority_margin", o["non_inferiority_margin"], problems, hi=1.0)
    if "sandbox" in doc and (o := _obj("sandbox", doc["sandbox"], problems)) is not None:
        _unknown("sandbox", o, {"template", "isolation", "egress"}, problems)
        for k in ("template", "isolation"):
            if k in o:
                _str(f"sandbox.{k}", o[k], problems)
        eg = o.get("egress", [])
        if not isinstance(eg, list) or not all(isinstance(x, str) and x for x in eg):
            problems.append("sandbox.egress: must be a list of non-empty strings")
    if "budgets" in doc and (o := _obj("budgets", doc["budgets"], problems)) is not None:
        _unknown("budgets", o, {"max_steps", "max_cost_usd", "max_tokens"}, problems)
        for k in ("max_steps", "max_tokens"):
            if k in o:
                _int(f"budgets.{k}", o[k], problems)
        if "max_cost_usd" in o:
            _num("budgets.max_cost_usd", o["max_cost_usd"], problems)
    if "guardrails" in doc and (o := _obj("guardrails", doc["guardrails"], problems)) is not None:
        _unknown("guardrails", o, {"mode", "policy"}, problems)
        if o.get("mode") not in ("off", "monitor", "enforce"):
            problems.append("guardrails.mode: required, one of off, monitor, enforce")
        if "policy" in o:
            _str("guardrails.policy", o["policy"], problems)


def normalize(doc: Any) -> dict[str, Any]:
    """Validate ``doc`` and return the canonical manifest (``tools.manifest_hash`` filled in).

    Raises :class:`AgentManifestError` listing every problem. A supplied ``version_id`` or
    ``tools.manifest_hash`` must agree with the content; a stale one is refused, not corrected.
    """
    problems: list[str] = []
    if not isinstance(doc, dict):
        raise AgentManifestError(["manifest: must be a JSON object"])
    for key in _REQUIRED:
        if key not in doc:
            problems.append(f"{key}: required component is missing")
    for key in sorted(set(doc) - set(_REQUIRED) - set(_OPTIONAL) - set(_ID_KEYS)):
        problems.append(f"{key}: unknown field")
    if "schema_version" in doc and doc["schema_version"] != SCHEMA_VERSION:
        problems.append(f"schema_version: must be {SCHEMA_VERSION}")
    if "agent" in doc:
        _str("agent", doc["agent"], problems, pattern=_NAME)
    if "code" in doc:
        _check_code(doc["code"], problems)
    if "prompts" in doc:
        _check_prompts(doc["prompts"], problems)
    if "models" in doc:
        _check_models(doc["models"], problems)
    if "tools" in doc:
        _check_tools(doc["tools"], problems)
    if "policy" in doc:
        _check_policy(doc["policy"], problems)
    _check_optional(doc, problems)
    if problems:
        raise AgentManifestError(problems)

    out = json.loads(canonical_json(doc))  # deep copy; also refuses NaN and non-JSON values
    out.pop("version_id", None)  # derived, never stored inside the content it hashes
    for m in out["models"]:
        if m["binding"] == "follow":
            m["alias"] = _MODEL_ALIAS[str(m["alias"]).lower()]
        else:
            m["version"] = str(m["version"])
    out["tools"].setdefault("grants", [])
    computed = tool_manifest_hash(out["tools"]["tools"])
    given = out["tools"].get("manifest_hash")
    if given is not None and given != computed:
        raise AgentManifestError(
            [f"tools.manifest_hash: {given} does not match the tool list ({computed})"]
        )
    out["tools"]["manifest_hash"] = computed
    out["tools"]["tools"] = sorted(out["tools"]["tools"], key=lambda t: t["name"])
    out["tools"]["grants"] = sorted(set(out["tools"]["grants"]))
    out["prompts"] = sorted(out["prompts"], key=lambda p: p["name"])
    out["models"] = sorted(out["models"], key=lambda m: m["role"])
    computed_id = version_id_of(out)
    if "version_id" in doc and doc["version_id"] != computed_id:
        raise AgentManifestError(
            [f"version_id: {doc['version_id']} does not match the content ({computed_id})"]
        )
    return out


# -- diff --------------------------------------------------------------------------------------

_KEYED = {"prompts": "name", "models": "role"}


def _flatten(m: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, val in m.items():
        if key in ("version_id", "schema_version"):
            continue
        if key in _KEYED:
            for item in val:
                flat[f"{key}:{item[_KEYED[key]]}"] = item
        elif key == "tools":
            for t in val["tools"]:
                flat[f"tools:{t['name']}"] = t["schema_hash"]
            flat["tools.grants"] = val["grants"]
        elif isinstance(val, dict):
            for k, v in val.items():
                flat[f"{key}.{k}"] = v
        else:
            flat[key] = val
    return flat


def diff_manifests(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Which components differ between two normalised manifests, by name. Pure.

    Each entry is ``{component, change: added|removed|changed, from, to}``; components are named
    ``prompts:<name>``, ``models:<role>``, ``tools:<tool>``, ``tools.grants`` or ``<section>.<field>``.
    """
    fa, fb = _flatten(a), _flatten(b)
    changes: list[dict[str, Any]] = []
    for name in sorted(set(fa) | set(fb)):
        if name not in fa:
            changes.append({"component": name, "change": "added", "from": None, "to": fb[name]})
        elif name not in fb:
            changes.append({"component": name, "change": "removed", "from": fa[name], "to": None})
        elif fa[name] != fb[name]:
            changes.append(
                {"component": name, "change": "changed", "from": fa[name], "to": fb[name]}
            )
    return {
        "from": version_id_of(a),
        "to": version_id_of(b),
        "identical": not changes,
        "changes": changes,
    }
