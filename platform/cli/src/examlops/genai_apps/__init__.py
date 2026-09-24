"""GenAI applications as registry artifacts (ADR 0159).

One typed, content-addressed, versioned manifest composing a gateway route, an optional RAG
binding, a prompt reference and a guardrail policy - with the alias/promotion machinery ADR 0146
gives agent versions, and the platform's one evaluation gate, not a second one.
"""

from examlops.genai_apps.components import (
    NOT_FOUND,
    UNREACHABLE,
    ComponentRefusal,
    component_refusals,
)
from examlops.genai_apps.manifest import (
    ALIASES,
    GUARDRAIL_MODES,
    RETRIEVAL_MODES,
    SCHEMA_VERSION,
    GenAIAppManifestError,
    canonical_json,
    diff_manifests,
    normalize,
    version_id_of,
)
from examlops.genai_apps.service import (
    GateRefusal,
    GenAIApplication,
    canonical_alias,
    diff,
    evidence_refusals,
    get,
    list_apps,
    model_key,
    register,
    resolve,
    rollback,
    set_alias,
)

__all__ = [
    "ALIASES",
    "GUARDRAIL_MODES",
    "NOT_FOUND",
    "RETRIEVAL_MODES",
    "SCHEMA_VERSION",
    "UNREACHABLE",
    "ComponentRefusal",
    "GateRefusal",
    "GenAIAppManifestError",
    "GenAIApplication",
    "canonical_alias",
    "canonical_json",
    "component_refusals",
    "diff",
    "diff_manifests",
    "evidence_refusals",
    "get",
    "list_apps",
    "model_key",
    "normalize",
    "register",
    "resolve",
    "rollback",
    "set_alias",
    "version_id_of",
]
