"""Pipeline-as-code: a typed Python DSL that compiles to a versioned IR (ADR 0080).

``dsl`` builds an IR by tracing, ``ir`` validates and hashes it, ``lowering`` maps a ``training``
IR onto the per-model YAML the existing generator already runs, ``loader`` reads pipeline files
and IR JSON. Stdlib-only and import-light; the heavy machinery is reached only at run time.
"""

from .dsl import (
    PipelineDef,
    Ref,
    Resources,
    custom_python,
    dataset,
    evaluate,
    hpo,
    pipeline,
    promote,
    step,
    train,
)
from .ir import (
    SCHEMA_VERSION,
    STEP_KINDS,
    IRError,
    build_ir,
    canonical_json,
    content_hash,
    topological_order,
    validate_ir,
)
from .lowering import Lowered, NotLowerableError, lower_training

__all__ = [
    "SCHEMA_VERSION",
    "STEP_KINDS",
    "IRError",
    "Lowered",
    "NotLowerableError",
    "PipelineDef",
    "Ref",
    "Resources",
    "build_ir",
    "canonical_json",
    "content_hash",
    "custom_python",
    "dataset",
    "evaluate",
    "hpo",
    "lower_training",
    "pipeline",
    "promote",
    "step",
    "topological_order",
    "train",
    "validate_ir",
]
