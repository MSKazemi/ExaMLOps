"""The reverse direction: a per-model registry YAML back into ``@pipeline`` DSL source (ADR 0080).

:mod:`examlops.pipeline_dsl.lowering` maps a ``training`` IR onto the per-model YAML. This module
maps that YAML back onto the DSL, so a model authored the ordinary way (YAML first, Phase 9/14) can
be *adopted* into pipeline-as-code without being retyped, and so the ADR's "the DSL round-trips to
it" claim is something the build can check rather than something the ADR asserts.

The contract is **fail loudly, never silently lossy**. Two independent gates enforce it:

1. **Partitioning.** Every top-level key of the YAML must land in exactly one of: the pipeline name,
   a ``train`` step param, the ``datasets`` list, the ``lifecycle`` of the ``promote`` step, the
   ``placement`` section (the train step's resources and the pipeline's target cluster), or the
   ``registry`` block the ``@pipeline`` decorator carries through (:data:`~.ir.REGISTRY_KEYS`).
   Anything else — an unknown key, an unknown dataset key, a value YAML parsed into something JSON
   cannot hold (an unquoted date is the common one) — raises :class:`NotRepresentableError` naming
   the construct. Nothing is dropped on a best-effort basis.
2. **Self-check.** The IR this module builds is lowered straight back with the *production*
   :func:`~.lowering.lower_training`, and the result must equal the canonicalised input mapping.
   A key that partitioning let through but lowering does not reproduce is a defect, and it is
   raised here instead of being written to a file.

Only after both gates pass is any source emitted. The emitted file is plain, ruff-clean Python that
``exa pipeline compile`` reads back.
"""

from __future__ import annotations

import keyword
import re
from typing import Any

from .ir import REGISTRY_KEYS, STEP_KINDS, IRError, _canon, build_ir, new_node
from .lowering import lower_training
from .placement import split_placement, validate_placement_block

__all__ = [
    "NotRepresentableError",
    "decompile_model_yaml",
    "ir_from_model_yaml",
    "render_pipeline_source",
]


class NotRepresentableError(IRError):
    """A registry YAML the DSL cannot express faithfully. The message names the construct."""


#: The ``train`` step's params, in the order the emitted source writes them. Kept as a tuple (the
#: kind's ``params`` is an unordered frozenset) so the generated file is byte-stable across runs.
_TRAIN_ORDER: tuple[str, ...] = ("model_class", "config_class", "task_type", "framework", "model")

#: The ``dataset`` step's params minus ``name`` (which the helper takes positionally), same reason.
_DATASET_ORDER: tuple[str, ...] = (
    "backend",
    "cache_dir",
    "batch_size",
    "columns",
    "input_features",
    "output_features",
    "splits",
    "dataplane",
    "feature_view",
)

#: Top-level YAML keys this module knows how to place. Everything else is refused by name.
#: ``name`` is the pipeline name; ``datasets``/``lifecycle`` become steps; the rest split between
#: the ``train`` step's params and the ``@pipeline`` registry block.
_KNOWN_TOP_LEVEL: frozenset[str] = (
    frozenset({"name", "datasets", "lifecycle", "placement"})
    | frozenset(_TRAIN_ORDER)
    | REGISTRY_KEYS
)

_IDENT = re.compile(r"[^0-9a-zA-Z_]+")

#: Names the emitted function body already binds. A dataset variable may not shadow one.
_TAKEN = frozenset(
    {"dataset", "evaluate", "pipeline", "promote", "train", "model", "metrics", "step"}
)


def _identifier(raw: str, *, fallback: str) -> str:
    """A safe lower-case Python identifier derived from ``raw`` (never a keyword, never empty)."""
    ident = _IDENT.sub("_", str(raw)).strip("_").lower()
    if not ident or ident[0].isdigit():
        ident = f"{fallback}_{ident}" if ident else fallback
    if keyword.iskeyword(ident) or keyword.issoftkeyword(ident):
        ident = f"{ident}_"
    return ident


def _require_json(value: Any, where: str) -> Any:
    """``_canon`` with a message that names *where* in the YAML the offending value sits."""
    try:
        return _canon(value, where)
    except IRError as exc:
        raise NotRepresentableError(
            f"{exc} — the DSL carries only JSON-shaped values; quote the value in the YAML "
            "(an unquoted date/time is the usual cause)"
        ) from exc


def _check_mapping(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise NotRepresentableError(
            f"a per-model registry YAML must be a mapping, got {type(raw).__name__}"
        )
    bad_keys = sorted(str(k) for k in raw if not isinstance(k, str))
    if bad_keys:
        raise NotRepresentableError(f"non-string top-level key(s) {bad_keys}")
    return dict(raw)


def _partition(raw: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Split the YAML into ``(name, train_params, registry)``, refusing anything unplaceable."""
    unknown = sorted(set(raw) - _KNOWN_TOP_LEVEL)
    if unknown:
        raise NotRepresentableError(
            f"the DSL has no place for top-level key(s) {unknown}: they are neither a train-step "
            f"param {sorted(_TRAIN_ORDER)}, the datasets/lifecycle steps, nor a registry section "
            f"{sorted(REGISTRY_KEYS)}. Refusing rather than writing a file that drops them."
        )
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise NotRepresentableError("the YAML needs a non-empty string 'name'")

    train_params = {k: _require_json(raw[k], f"{k!r}") for k in _TRAIN_ORDER if k in raw}
    missing = sorted(STEP_KINDS["train"].required_params - set(train_params))
    if missing:
        raise NotRepresentableError(
            f"the DSL's train step requires {missing}; the YAML does not define it"
        )
    registry = {k: _require_json(raw[k], f"{k!r}") for k in raw if k in REGISTRY_KEYS}
    return name, train_params, registry


def _datasets(raw: dict[str, Any]) -> list[dict[str, Any]]:
    entries = raw.get("datasets") or []
    if not isinstance(entries, list) or not entries:
        raise NotRepresentableError(
            "the DSL's train step consumes at least one dataset; the YAML defines none "
            "(a model with no 'datasets' cannot be expressed as a flow)"
        )
    allowed = STEP_KINDS["dataset"].params
    out: list[dict[str, Any]] = []
    for pos, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise NotRepresentableError(f"datasets[{pos}] must be a mapping")
        ds_name = entry.get("name")
        if not isinstance(ds_name, str) or not ds_name.strip():
            raise NotRepresentableError(f"datasets[{pos}] needs a non-empty string 'name'")
        extra = sorted(set(map(str, entry)) - allowed)
        if extra:
            raise NotRepresentableError(
                f"dataset {ds_name!r}: the DSL's dataset step has no param(s) {extra} "
                f"(it accepts {sorted(allowed)})"
            )
        out.append({k: _require_json(v, f"datasets[{ds_name}].{k}") for k, v in entry.items()})
    return out


def _placement(raw: dict[str, Any]) -> tuple[str | None, dict[str, int]]:
    """The ``placement:`` section as ``(target cluster, train-step resources)``, or refuse."""
    if "placement" not in raw:
        return None, {}
    block = _require_json(raw["placement"], "'placement'")
    problems = validate_placement_block(block)
    if problems:
        raise NotRepresentableError(f"'placement' is invalid: {'; '.join(problems)}")
    if not block:
        raise NotRepresentableError(
            "'placement' is empty; remove the key (an empty section states nothing, and the "
            "DSL twin would not reproduce it)"
        )
    return split_placement(block)


def ir_from_model_yaml(raw: Any) -> dict[str, Any]:
    """Build the IR a ``@pipeline`` twin of ``raw`` would compile to, or raise.

    Refuses anything the DSL cannot hold, then verifies the IR by lowering it back through the
    production lowering and comparing with the canonicalised input.
    """
    mapping = _check_mapping(raw)
    name, train_params, registry = _partition(mapping)
    ds_entries = _datasets(mapping)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    ds_ids: list[str] = []
    used: set[str] = set()
    for entry in ds_entries:
        stem = f"dataset_{entry['name']}"
        node_id, n = stem, 2
        while node_id in used:
            node_id, n = f"{stem}_{n}", n + 1
        used.add(node_id)
        ds_ids.append(node_id)
        nodes.append(new_node(node_id, "dataset", entry))
    target_cluster, train_resources = _placement(mapping)
    nodes.append(new_node("train", "train", train_params, train_resources))
    edges += [{"from": d, "output": "data", "to": "train", "input": "datasets"} for d in ds_ids]
    nodes.append(new_node("evaluate", "evaluate", {}))
    edges.append({"from": "train", "output": "model", "to": "evaluate", "input": "model"})

    if "lifecycle" in mapping:
        lifecycle = _require_json(mapping["lifecycle"], "'lifecycle'")
        if not isinstance(lifecycle, list):
            raise NotRepresentableError("'lifecycle' must be a list of stage mappings")
        nodes.append(new_node("promote", "promote", {"lifecycle": lifecycle}))
        edges.append({"from": "evaluate", "output": "metrics", "to": "promote", "input": "metrics"})

    doc = build_ir(
        name=name,
        kind="training",
        nodes=nodes,
        edges=edges,
        registry=registry,
        target={"cluster": target_cluster} if target_cluster else None,
    )
    _verify_round_trip(doc, mapping)
    return doc


def _verify_round_trip(doc: dict[str, Any], mapping: dict[str, Any]) -> None:
    """Lower the freshly built IR and insist it reproduces the input mapping exactly."""
    lowered = lower_training(doc).model_yaml
    original = _require_json(mapping, "the YAML document")
    if lowered == original:
        return
    diff = sorted(
        k for k in set(lowered) | set(original) if lowered.get(k, ...) != original.get(k, ...)
    )
    raise NotRepresentableError(
        f"the DSL twin does not reproduce the YAML: key(s) {diff} differ after lowering. "
        "Refusing to write a file that is not an exact twin of the input."
    )


# ── source emission ───────────────────────────────────────────────────────────────────────────

_WIDTH = 98


def _flat(value: Any) -> str:
    """One-line Python literal for a JSON-shaped value, preferring double-quoted strings."""
    if isinstance(value, str):
        rendered = repr(value)
        return '"' + rendered[1:-1] + '"' if rendered.startswith("'") else rendered
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_flat(k)}: {_flat(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_flat(v) for v in value) + "]"
    return repr(value)


def _literal(value: Any, indent: int, col: int) -> str:
    """``value`` as source: one line when it fits at column ``col``, else wrapped under ``indent``.

    ``indent`` is the *statement's* indentation, so continuation lines land at ``indent + 4`` the
    way a formatter would put them; ``col`` only decides whether wrapping is needed at all.
    """
    flat = _flat(value)
    if col + len(flat) <= _WIDTH:
        return flat
    pad, close = " " * (indent + 4), " " * indent
    if isinstance(value, dict) and value:
        body = "\n".join(
            f"{pad}{_flat(k)}: {_literal(v, indent + 4, indent + 4 + len(_flat(k)) + 2)},"
            for k, v in value.items()
        )
        return "{\n" + body + "\n" + close + "}"
    if isinstance(value, list) and value:
        body = "\n".join(f"{pad}{_literal(v, indent + 4, indent + 4)}," for v in value)
        return "[\n" + body + "\n" + close + "]"
    return flat


def _call(head: str, args: list[str], indent: int, col: int) -> str:
    """``head(a, b)`` on one line when it fits at ``col``, otherwise one argument per line."""
    one = f"{head}({', '.join(args)})"
    if "\n" not in one and col + len(one) <= _WIDTH:
        return one
    pad = " " * (indent + 4)
    return f"{head}(\n" + "".join(f"{pad}{a},\n" for a in args) + " " * indent + ")"


def _kwargs(params: dict[str, Any], order: tuple[str, ...], indent: int) -> list[str]:
    """``k=<literal>`` for each present param: the declared order first, then any newcomer.

    Rendered for the wrapped form (one argument per line at ``indent + 4``), which is also what
    :func:`_call` measures before deciding they all fit on one line after all.
    """
    keys = [k for k in order if k in params] + sorted(set(params) - set(order))
    return [f"{k}={_literal(params[k], indent + 4, indent + 4 + len(k) + 1)}" for k in keys]


def _docstring_safe(text: str) -> str:
    """Keep an arbitrary name or path from terminating the generated module docstring."""
    return str(text).replace('"""', "'-'-'")


def decompile_model_yaml(raw: Any, *, source: str = "a registry YAML") -> str:
    """Return ``@pipeline`` DSL source equivalent to the registry YAML mapping ``raw``.

    ``source`` only appears in the generated module docstring. Raises
    :class:`NotRepresentableError` — never returns a partial file — when the YAML holds anything
    the DSL cannot express.
    """
    return render_pipeline_source(ir_from_model_yaml(raw), source=source)


def render_pipeline_source(doc: dict[str, Any], *, source: str = "a registry YAML") -> str:
    """Emit ``@pipeline`` DSL source for a ``training`` IR built by :func:`ir_from_model_yaml`."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for node in doc["nodes"]:
        by_kind.setdefault(node["kind"], []).append(node)

    name = doc["name"]
    fn_name = _identifier(name, fallback="flow")
    helpers = {"dataset", "evaluate", "pipeline", "train"}
    if "promote" in by_kind:
        helpers.add("promote")

    lines: list[str] = [
        f'"""{_docstring_safe(name)} as pipeline-as-code (ADR 0080).',
        "",
        f"Decompiled from {_docstring_safe(source)} by `exa pipeline decompile`. It is an exact",
        "twin of that YAML: compiling it reproduces the same registry mapping, so either file may",
        "be the one you keep.",
        "",
        "    exa pipeline compile <this file> --yaml <the model YAML>",
        "",
        "This file is trusted-tier Python: `exa pipeline compile` executes it.",
        '"""',
        "",
        f"from examlops.sdk import {', '.join(sorted(helpers))}",
        "",
        "",
    ]

    decorator_args = [f"name={_flat(name)}"]
    if (doc.get("target") or {}).get("cluster"):
        decorator_args.append(f"cluster={_flat(doc['target']['cluster'])}")
    decorator_args += _kwargs(doc["registry"], tuple(doc["registry"]), 0)
    lines.append("@" + _call("pipeline", decorator_args, 0, 1))
    lines.append(f"def {fn_name}():")

    var_of: dict[str, str] = {}
    taken = set(_TAKEN) | {fn_name}
    for node in by_kind["dataset"]:
        ds_name = node["params"]["name"]
        stem = _identifier(ds_name, fallback="ds")
        var, n = stem, 2
        while var in taken:
            var, n = f"{stem}_{n}", n + 1
        taken.add(var)
        var_of[node["id"]] = var
        args = [_flat(ds_name)] + _kwargs(
            {k: v for k, v in node["params"].items() if k != "name"}, _DATASET_ORDER, 4
        )
        lines.append(f"    {var} = " + _call("dataset", args, 4, 4 + len(var) + 3))

    train_node = by_kind["train"][0]
    train_args = [var_of[e["from"]] for e in doc["edges"] if e["to"] == "train"]
    train_args += _kwargs(train_node["params"], _TRAIN_ORDER, 4)
    if train_node.get("resources"):
        train_args.append(f"resources={_literal(train_node['resources'], 8, 18)}")
    lines.append("    model = " + _call("train", train_args, 4, 12))
    lines.append("    metrics = evaluate(model)")
    if "promote" in by_kind:
        lifecycle = by_kind["promote"][0]["params"]["lifecycle"]
        args = ["metrics", f"lifecycle={_literal(lifecycle, 8, 18)}"]
        lines.append("    " + _call("promote", args, 4, 4))

    return "\n".join(lines) + "\n"
