"""``policy`` provider domain — pluggable policy engines (ADR 0079 decision 4, ADR 0029).

A third-party engine (Cedar, an in-house PDP, ...) registers under the ``exa.providers.policy``
entry-point group, exactly like every other ``exa.providers.<domain>`` group. Its class is a
:class:`~examlops.providers.Provider` whose ``compute`` receives one policy request and returns
``{"allow": bool, "effect": "allow|deny|require_approval", "reasons": [str, ...]}``::

    inputs = {"decision": "supply_chain", "action": "deploy", "subject": ..., "resource": ...,
              "tenant": ..., "context": {...}}

It is selected with ``EXAMLOPS_POLICY_ENGINE=<name>``. A plugin that cannot be resolved or loaded
degrades — audibly, via ``degraded_to_default`` — to the built-in YAML engine, and a broken plugin
is listed with its error in ``exa providers list --domain policy``.

The two built-ins are descriptors so the domain is discoverable; the actual engines remain
:class:`~examlops.policy_engine.YamlPolicyEngine` / ``RegoPolicyEngine``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from examlops.providers import Provider, ProviderMeta, register_provider

DOMAIN = "policy"


class YamlEngineProvider(Provider):
    """The default engine: ``policy.yaml`` rules over sandboxed ``simpleeval`` conditions."""

    name = "yaml"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology="Ordered policy.yaml rules; first match wins; sandboxed simpleeval.",
            outputs=("allow", "effect", "reasons"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        from examlops.policy import decide

        ctx = dict(inputs.get("context") or {})
        d = decide(str(inputs.get("action", "")), ctx, audit=False)
        return {"allow": d.allowed, "effect": d.effect, "reasons": [d.reason]}


class OpaEngineProvider(Provider):
    """OPA/Rego via the ``opa`` binary (``EXAMLOPS_POLICY_ENGINE=opa``)."""

    name = "opa"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology="opa eval against data.examlops.<decision>.allow in a Rego bundle.",
            outputs=("allow", "effect", "reasons"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        from examlops.policy_engine import PolicyInput, RegoPolicyEngine, get_engine

        engine = get_engine()
        if not isinstance(engine, RegoPolicyEngine):
            raise RuntimeError("opa engine unavailable (binary missing or not selected)")
        pin = PolicyInput(
            action=str(inputs.get("action", "")),
            subject=inputs.get("subject"),
            resource=inputs.get("resource"),
            tenant=str(inputs.get("tenant", "default")),
            context=dict(inputs.get("context") or {}),
        )
        r = engine.evaluate(str(inputs.get("decision", "")), pin)
        return {"allow": r.allow, "effect": r.effect, "reasons": r.reasons}


register_provider(DOMAIN, "yaml", YamlEngineProvider, default=True)
register_provider(DOMAIN, "opa", OpaEngineProvider)
