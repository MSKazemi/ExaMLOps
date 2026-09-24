"""The ``GenAIApplication`` manifest (ADR 0159 decision 1): typed, canonical, content-addressed.

A GenAI application is a *composition* — a gateway route, an optional RAG binding, a prompt and a
guardrail policy — and the whole point of registering it is that the **combination** is versioned,
not just its four ingredients. So this module validates the composition strictly and hashes it.

It mirrors :mod:`examlops.agent_versions.manifest` deliberately, and *reuses* its
:func:`~examlops.agent_versions.manifest.canonical_json` rather than redefining byte-stable JSON a
second time. What differs is only what the two objects are: an ``AgentVersion`` pins a runtime
(image digest, tool schema hashes, autonomy level); a ``GenAIApplication`` names four references
and nothing else.

Two references are deliberately floating, and for the same reason ADR 0146 decision 2 gives a
``follow`` model binding: the object they name is independently versioned and aliased by its own
registry, so re-pointing it is that registry's gated change, not a new application version.

* ``route.model`` — a gateway *logical model* name (ADR 0151 d1 / ADR 0155 ``models.<name>``).
  Re-pointing it is a ``gateway.yaml`` change, atomic and validated there.
* ``prompt.label`` — the ADR 0009 label, whose move is already gated by that registry.

``prompt.version`` is the pinned alternative, and the two are mutually exclusive: a manifest that
carries neither says nothing at all about which prompt it runs, so it is refused rather than
defaulted.

Everything here is pure - no database, no network.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from examlops.agent_versions.manifest import ALIASES, canonical_json

__all__ = [
    "ALIASES",
    "GUARDRAIL_MODES",
    "RETRIEVAL_MODES",
    "SCHEMA_VERSION",
    "GenAIAppManifestError",
    "canonical_json",
    "diff_manifests",
    "normalize",
    "version_id_of",
]

SCHEMA_VERSION = 1
#: Spelled exactly as ADR 0146 spells them, so the two registries read as one family.
ALIASES = ALIASES
#: ``examlops.rag.RETRIEVAL_MODES`` — this module must never grow a second vocabulary.
RETRIEVAL_MODES = ("dense", "hybrid")
#: ``examlops.guardrails`` vocabulary (ADR 0026), unchanged.
GUARDRAIL_MODES = ("off", "monitor", "enforce")

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SUITE = re.compile(r"^[A-Za-z0-9._-]+@[0-9]+$")
_KEY_REF = re.compile(r"^gateway/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: A URL, a host:port or anything with a scheme is a *provider address*, not a logical model.
_LOOKS_LIKE_A_HOST = re.compile(r"(^[a-zA-Z][a-zA-Z0-9+.-]*://)|(^\S+:\d{2,5}(/|$))|(\s)")

_REQUIRED = ("schema_version", "name", "route", "prompt", "guardrail")
_OPTIONAL = ("rag", "budgets", "eval")
_ID_KEYS = ("version_id",)

_DEFAULT_TOP_K = 5
_DEFAULT_RETRIEVAL = "dense"
_DEFAULT_POLICY = "default"


class GenAIAppManifestError(ValueError):
    """The manifest is not a valid ``GenAIApplication``; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def version_id_of(manifest: dict[str, Any]) -> str:
    """``gaa-sha256:<hex>`` over the normalised manifest (``version_id`` itself excluded).

    The same scheme ``examlops.agent_versions.manifest.version_id_of`` uses for ``av-…``, with a
    different prefix so the two id spaces can never be confused in a log line or an alias row.
    """
    import hashlib

    body = {k: v for k, v in manifest.items() if k not in _ID_KEYS}
    return "gaa-sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# -- validation --------------------------------------------------------------------------------


def _unknown(where: str, obj: dict[str, Any], allowed: set[str], problems: list[str]) -> None:
    for key in sorted(set(obj) - allowed):
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


def _check_route(v: Any, problems: list[str]) -> None:
    o = _obj("route", v, problems)
    if o is None:
        return
    _unknown("route", o, {"model", "key_ref"}, problems)
    if _str("route.model", o.get("model"), problems):
        if _LOOKS_LIKE_A_HOST.search(o["model"]):
            problems.append(
                "route.model: must be a gateway logical-model name, not a provider address - "
                "resolution, credentials and locality stay the gateway's (ADR 0151 d1)"
            )
    if "key_ref" not in o:
        problems.append("route.key_ref: required (a 'gateway/<key-name>' reference)")
    elif _str("route.key_ref", o["key_ref"], problems, pattern=_KEY_REF):
        pass


def _check_rag(v: Any, problems: list[str]) -> None:
    o = _obj("rag", v, problems)
    if o is None:
        return
    _unknown("rag", o, {"kb", "retrieval", "top_k", "encoder"}, problems)
    _str("rag.kb", o.get("kb"), problems)
    if "retrieval" in o and o["retrieval"] not in RETRIEVAL_MODES:
        problems.append(f"rag.retrieval: must be one of {', '.join(RETRIEVAL_MODES)}")
    if "top_k" in o:
        _int("rag.top_k", o["top_k"], problems)
    if "encoder" in o:
        _str("rag.encoder", o["encoder"], problems)


def _check_prompt(v: Any, problems: list[str]) -> None:
    o = _obj("prompt", v, problems)
    if o is None:
        return
    _unknown("prompt", o, {"name", "label", "version"}, problems)
    _str("prompt.name", o.get("name"), problems)
    has_label, has_version = "label" in o, "version" in o
    if has_label and has_version:
        problems.append(
            "prompt: 'label' and 'version' are mutually exclusive - a label follows the registry, "
            "a version pins it (ADR 0009)"
        )
    elif not has_label and not has_version:
        problems.append(
            "prompt: one of 'label' (floating, follows the registry) or 'version' (pinned) is "
            "required - an unqualified prompt reference is refused"
        )
    if has_label:
        _str("prompt.label", o["label"], problems)
    if has_version:
        _int("prompt.version", o["version"], problems)


def _check_guardrail(v: Any, problems: list[str]) -> None:
    o = _obj("guardrail", v, problems)
    if o is None:
        return
    _unknown("guardrail", o, {"mode", "policy"}, problems)
    if o.get("mode") not in GUARDRAIL_MODES:
        problems.append(f"guardrail.mode: required, one of {', '.join(GUARDRAIL_MODES)}")
    if "policy" in o:
        _str("guardrail.policy", o["policy"], problems)


def _check_optional(doc: dict[str, Any], problems: list[str]) -> None:
    if "budgets" in doc and (o := _obj("budgets", doc["budgets"], problems)) is not None:
        _unknown("budgets", o, {"max_tokens", "max_cost_usd"}, problems)
        if "max_tokens" in o:
            _int("budgets.max_tokens", o["max_tokens"], problems)
        if "max_cost_usd" in o:
            _num("budgets.max_cost_usd", o["max_cost_usd"], problems)
    if "eval" in doc and (o := _obj("eval", doc["eval"], problems)) is not None:
        _unknown("eval", o, {"suites"}, problems)
        suites = o.get("suites")
        if not isinstance(suites, list) or not suites:
            problems.append("eval.suites: must be a non-empty list of name@version")
        else:
            for i, s in enumerate(suites):
                _str(f"eval.suites[{i}]", s, problems, pattern=_SUITE)


def normalize(doc: Any) -> dict[str, Any]:
    """Validate ``doc`` and return the canonical manifest (defaults filled in).

    Raises :class:`GenAIAppManifestError` listing every problem. A supplied ``version_id`` must
    agree with the content; a stale one is refused, not corrected.

    Defaults are filled *before* hashing, so a manifest that spells ``top_k: 5`` and one that omits
    it are the same version - the content is what the application does, not how it was typed.
    """
    problems: list[str] = []
    if not isinstance(doc, dict):
        raise GenAIAppManifestError(["manifest: must be a JSON object"])
    for key in _REQUIRED:
        if key not in doc:
            problems.append(f"{key}: required component is missing")
    for key in sorted(set(doc) - set(_REQUIRED) - set(_OPTIONAL) - set(_ID_KEYS)):
        problems.append(f"{key}: unknown field")
    if "schema_version" in doc and doc["schema_version"] != SCHEMA_VERSION:
        problems.append(f"schema_version: must be {SCHEMA_VERSION}")
    if "name" in doc:
        _str("name", doc["name"], problems, pattern=_NAME)
    if "route" in doc:
        _check_route(doc["route"], problems)
    if "rag" in doc:
        _check_rag(doc["rag"], problems)
    if "prompt" in doc:
        _check_prompt(doc["prompt"], problems)
    if "guardrail" in doc:
        _check_guardrail(doc["guardrail"], problems)
    _check_optional(doc, problems)
    if problems:
        raise GenAIAppManifestError(problems)

    out = json.loads(canonical_json(doc))  # deep copy; also refuses NaN and non-JSON values
    out.pop("version_id", None)  # derived, never stored inside the content it hashes
    out["guardrail"].setdefault("policy", _DEFAULT_POLICY)
    if "rag" in out:
        out["rag"].setdefault("retrieval", _DEFAULT_RETRIEVAL)
        out["rag"].setdefault("top_k", _DEFAULT_TOP_K)
    if "eval" in out:
        out["eval"]["suites"] = sorted(set(out["eval"]["suites"]))
    computed_id = version_id_of(out)
    if "version_id" in doc and doc["version_id"] != computed_id:
        raise GenAIAppManifestError(
            [f"version_id: {doc['version_id']} does not match the content ({computed_id})"]
        )
    return out


# -- diff --------------------------------------------------------------------------------------


def _flatten(m: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, val in m.items():
        if key in ("version_id", "schema_version"):
            continue
        if key == "eval":
            flat["eval.suites"] = val["suites"]
        elif isinstance(val, dict):
            for k, v in val.items():
                flat[f"{key}.{k}"] = v
        else:
            flat[key] = val
    return flat


def diff_manifests(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Which components differ between two normalised manifests, by name. Pure.

    Each entry is ``{component, change: added|removed|changed, from, to}``; components are named
    ``<section>.<field>`` (``route.model``, ``rag.kb``, ``guardrail.mode``, ...).
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
