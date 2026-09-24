"""Does every component this manifest *declares* currently resolve? (ADR 0159 decisions 3 & 5).

Partial composition is legal - a manifest with no ``rag`` block is a perfectly good application.
A declared component that cannot be resolved is not. ADR 0026 records the gateway going live with
no guardrail call at all because "call the guardrail" was something each caller had to remember;
the whole reason a ``GenAIApplication`` exists is that the composition is one object with one
answer, so the answer here is never "skip it".

Two typed outcomes, both refusals, never a shrug:

``component_not_found``
    The component resolved cleanly to *nothing*: no such gateway route, no such knowledge base,
    no such prompt, no such guardrail policy.
``component_unreachable``
    The subsystem could not be asked. A read that raised, a config file that will not parse, a
    module that will not import. **This is a refusal too** - an unanswerable question about a
    guardrail is not a pass.

Every check is a *read*: nothing here calls a model, spends budget, or writes a row. The one
declared reference this module deliberately does **not** resolve is ``route.key_ref``: ADR 0154 d1
says a credential is resolved at use and never at rest, so reaching into the secret store at
promotion time would be the opposite of that decision, not an extra guarantee.

(Named ``components`` rather than ``resolve``: the package already exports a ``resolve(name,
alias)`` function, and a submodule sharing that name is shadowed by it on the package object -
so ``examlops.genai_apps.resolve`` would silently mean the function, not this module.)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

__all__ = [
    "NOT_FOUND",
    "UNREACHABLE",
    "ComponentRefusal",
    "component_refusals",
    "known_guardrail_policies",
]

NOT_FOUND = "component_not_found"
UNREACHABLE = "component_unreachable"

#: The guardrail policies :mod:`examlops.guardrails` can actually realise today. ADR 0026 describes
#: per-tenant policies as a *constructor argument set*, not a registry - there is no
#: ``get_policy(name)`` anywhere in the tree - so exactly one policy, the built-in default, is
#: resolvable. Any other name is refused rather than quietly accepted, which is the whole point:
#: a manifest naming ``policy: strict`` today would otherwise promote to Production and behave
#: exactly like ``default``. When ADR 0026's named-policy registry lands, this is the one function
#: that changes.
_BUILTIN_GUARDRAIL_POLICY = "default"


@dataclass(frozen=True)
class ComponentRefusal:
    """One declared component that does not currently resolve."""

    code: str  # NOT_FOUND | UNREACHABLE
    component: str  # "route.model", "rag.kb", "prompt", "guardrail.policy"
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.component}: {self.detail}"


def known_guardrail_policies() -> list[str]:
    """The guardrail policy names that resolve. Raises when the subsystem cannot be asked."""
    from examlops.guardrails import (
        DefaultGuardrail,  # noqa: F401 - import IS the reachability check
    )

    return [_BUILTIN_GUARDRAIL_POLICY]


# -- per-component checks ----------------------------------------------------------------------


def _gateway_route_names() -> set[str]:
    """Every logical model / alias the gateway would resolve, read without calling anything.

    ``gateway.yaml`` is the single source of truth when a site has one (ADR 0155 d1), so its
    ``models`` and ``aliases`` are read first - statically, with the gateway's own validator, and
    without ``build_runtime``, which is async and opens real provider connections. The in-process
    router (what ``exa gateway chat`` resolves against today) is unioned in so a route that exists
    only there is not reported as missing.
    """
    from examlops.gateway import build_default_router
    from examlops.gateway.config import default_config_path, load_config_file, validate_config

    names: set[str] = set()
    path = default_config_path()
    if path is not None:
        raw = load_config_file(path)  # raises ConfigError
        errors = validate_config(raw)
        if errors:
            raise RuntimeError(f"{path} is invalid: {errors[0]}")
        for section in ("models", "aliases"):
            block = raw.get(section) or {}
            if isinstance(block, dict):
                names |= {str(k) for k in block}
    names |= set(build_default_router().routes)
    names.add(os.getenv("EXAMLOPS_GATEWAY_DEFAULT_MODEL", "default"))
    return names


def _check_route(manifest: dict[str, Any]) -> list[ComponentRefusal]:
    model = manifest["route"]["model"]
    try:
        names = _gateway_route_names()
    except Exception as exc:  # noqa: BLE001 - an unanswerable question is a refusal, not a pass
        return [
            ComponentRefusal(
                UNREACHABLE,
                "route.model",
                f"the gateway routing table could not be read ({type(exc).__name__}: {exc})",
            )
        ]
    if model in names:
        return []
    return [
        ComponentRefusal(
            NOT_FOUND,
            "route.model",
            f"no gateway route named {model!r} (see: exa gateway status)",
        )
    ]


def _check_rag(manifest: dict[str, Any], *, tenant: str) -> list[ComponentRefusal]:
    rag = manifest.get("rag")
    if rag is None:
        return []  # RAG is additive: not declared is not missing (ADR 0159 decision 5)
    kb = rag["kb"]
    try:
        from examlops.data import get_db, init_db

        init_db()
        with get_db() as conn:
            row = conn.execute(
                "SELECT encoder FROM rag_kbs WHERE kb=? AND tenant=?", (kb, tenant)
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return [
            ComponentRefusal(
                UNREACHABLE,
                "rag.kb",
                f"the knowledge-base registry could not be read ({type(exc).__name__}: {exc})",
            )
        ]
    if row is None:
        return [
            ComponentRefusal(
                NOT_FOUND,
                "rag.kb",
                f"no knowledge base {kb!r} for tenant {tenant!r} (see: exa rag list)",
            )
        ]
    declared = rag.get("encoder")
    stamped = str(row["encoder"]) if row["encoder"] else None
    if declared and stamped and declared != stamped:
        return [
            ComponentRefusal(
                NOT_FOUND,
                "rag.encoder",
                f"knowledge base {kb!r} was ingested with encoder {stamped!r}, not {declared!r} "
                "- a query under a different encoder scores vectors that mean nothing (ADR 0043)",
            )
        ]
    return []


def _check_prompt(manifest: dict[str, Any]) -> list[ComponentRefusal]:
    """Read the registry directly, never :func:`examlops.prompts.get_prompt`.

    That function is a *serving* resolver: it keeps a 30-second last-known-good cache so a brief
    registry outage does not break a live call. Exactly that fallback would let a deleted prompt
    pass this gate, so the gate reads the store.
    """
    prompt = manifest["prompt"]
    name = prompt["name"]
    try:
        from examlops.data.prompts import get_prompt_by_label, get_prompt_version

        if "version" in prompt:
            row = get_prompt_version(name, int(prompt["version"]))
            ref = f"{name}@v{prompt['version']}"
        else:
            row = get_prompt_by_label(name, str(prompt["label"]))
            ref = f"{name}@{prompt['label']}"
    except Exception as exc:  # noqa: BLE001
        return [
            ComponentRefusal(
                UNREACHABLE,
                "prompt",
                f"the prompt registry could not be read ({type(exc).__name__}: {exc})",
            )
        ]
    if row is None:
        return [
            ComponentRefusal(
                NOT_FOUND, "prompt", f"no prompt {ref} in the registry (see: exa prompt list)"
            )
        ]
    return []


def _check_guardrail(manifest: dict[str, Any]) -> list[ComponentRefusal]:
    policy = manifest["guardrail"]["policy"]
    try:
        known = known_guardrail_policies()
    except Exception as exc:  # noqa: BLE001 - the one case ADR 0026 spent an amendment closing
        return [
            ComponentRefusal(
                UNREACHABLE,
                "guardrail.policy",
                f"the guardrail subsystem could not be reached ({type(exc).__name__}: {exc}) - "
                "an application whose guardrail cannot be resolved is refused, never run unguarded",
            )
        ]
    if policy in known:
        return []
    return [
        ComponentRefusal(
            NOT_FOUND,
            "guardrail.policy",
            f"no guardrail policy {policy!r}; this platform realises {known} "
            "- a named policy that does not exist would run as the default, unannounced",
        )
    ]


def component_refusals(
    manifest: dict[str, Any], *, tenant: str = "default"
) -> list[ComponentRefusal]:
    """Every declared component of ``manifest`` that does not currently resolve.

    An empty list means every reference was checked and answered. It never means a check was
    skipped: a component that could not be asked about comes back as ``component_unreachable``.
    """
    return [
        *_check_route(manifest),
        *_check_rag(manifest, tenant=tenant),
        *_check_prompt(manifest),
        *_check_guardrail(manifest),
    ]
