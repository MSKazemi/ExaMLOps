"""OpenFGA authorization-model export (Phase 2 item 2.5 — RBAC → OpenFGA).

The audit wants security-critical stores lifted OUT of the shared SQLite monolith: RBAC to a dedicated
OpenFGA service. This module emits the OpenFGA **authorization model** (both the ``.fga`` DSL and the
JSON API shape) that mirrors ExaMLOps's existing relation model — ``owner ⊇ editor ⊇ viewer`` over
hierarchical ``project`` → ``model``/``pipeline``/… objects — so the D6 `authz_relations` can be
migrated to OpenFGA with an equivalent model instead of a hand-authored one that could drift. The
relation *tuples* export (from the live `authz_relations`) rides on top; this is the schema.

Pure (no OpenFGA client) → testable; `exa` / a migration job feeds the output to `fga model write`.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1.1"

# Child object types that inherit their grants from a parent `project` (spec R6).
_CHILD_TYPES = ("model", "pipeline", "serving_endpoint", "connection", "dataset", "storage")


def _relations_for(child: bool) -> dict[str, Any]:
    """Relation definitions with owner⊇editor⊇viewer + (for children) parent inheritance."""

    def _union(direct: str, implied: list[str]) -> dict[str, Any]:
        children: list[dict[str, Any]] = [{"this": {}}]
        children += [{"computedUserset": {"relation": r}} for r in implied]
        if child:  # inherit the same relation from the parent project (spec R6)
            children.append(
                {
                    "tupleToUserset": {
                        "tupleset": {"relation": "parent"},
                        "computedUserset": {"relation": direct},
                    }
                }
            )
        return {"union": {"child": children}}

    rels: dict[str, Any] = {}
    if child:
        rels["parent"] = {"this": {}}
    rels["owner"] = _union("owner", [])
    rels["editor"] = _union("editor", ["owner"])
    rels["viewer"] = _union("viewer", ["editor"])
    return rels


def authorization_model() -> dict[str, Any]:
    """The OpenFGA JSON authorization model mirroring the D6 relation hierarchy (item 2.5)."""
    type_defs: list[dict[str, Any]] = [
        {"type": "user", "relations": {}},
        {"type": "project", "relations": _relations_for(child=False)},
    ]
    for t in _CHILD_TYPES:
        type_defs.append({"type": t, "relations": _relations_for(child=True)})
    return {"schema_version": SCHEMA_VERSION, "type_definitions": type_defs}


def to_dsl() -> str:
    """The equivalent OpenFGA ``.fga`` DSL (human-authored/reviewed form)."""
    lines = [
        "model",
        f"  schema {SCHEMA_VERSION}",
        "",
        "type user",
        "",
        "type project",
        "  relations",
        "    define owner: [user]",
        "    define editor: [user] or owner",
        "    define viewer: [user] or editor",
    ]
    for t in _CHILD_TYPES:
        lines += [
            "",
            f"type {t}",
            "  relations",
            "    define parent: [project]",
            "    define owner: [user] or owner from parent",
            "    define editor: [user] or owner or editor from parent",
            "    define viewer: [user] or editor or viewer from parent",
        ]
    return "\n".join(lines) + "\n"


def relation_tuples() -> list[dict[str, str]]:
    """Export the live D6 relations as OpenFGA tuples ``{user, relation, object}`` (for `fga tuple write`)."""
    from examlops.data import init_db
    from examlops.data.governance import list_relations

    init_db()
    out: list[dict[str, str]] = []
    for r in list_relations():
        # authz objects are 'type:id' already (e.g. 'project:acme', 'model:JPCP').
        out.append(
            {"user": f"user:{r['subject']}", "relation": r["relation"], "object": r["object"]}
        )
    return out
