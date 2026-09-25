"""Pulling a catalog entry into a project (ADR 0158 Phase 2, decision 2).

A pull **materializes the platform's own per-model YAML** — the exact schema
``usecases/*/models/*.yaml`` already defines — assigns the new model to the project through the
existing ``project_resources(kind='model')`` join (ADR 0086), records the pull, and emits one
lineage edge ``catalog_entry -> model`` through :func:`examlops.lineage.emit_lineage`. That is
all it does.

**A pull performs no training and no deployment.** It creates no MLflow registered model, no
version, no alias, and starts no serving replica: the result is a normal, editable model
definition with no training run yet, indistinguishable from one an operator hand-wrote. The
catalog answers "what could I start from?"; the registry keeps answering "what did we produce?"
(ADR 0158 decision 4), and the ``catalog_pulls`` row is the only thread between them — a lineage
record, not a live binding.

Rendering is deliberately Jinja-free (spec §1.1): a template is plain YAML with ``${placeholder}``
substitutions, so a curated template is readable as the file it will become and carries no
executable surface.

**Where a template lives.** A recipe template renders into the active use-case pack's ``models/``
directory and names that pack's datasets and config shims, so it is *pack* content (ADR 0094), not
platform code — the platform ships no recipe template and could not, without naming a concrete
dataset. The one template the package does own is the use-case-agnostic ``base_model`` skeleton
below, which is code, not a data file. Resolution never reaches for the repository checkout: a
wheel or an image has none around it (ADR 0129 §8). See :func:`_template_roots`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

import yaml

from examlops.catalog.manifest import CatalogEntry
from examlops.catalog.store import CatalogSignatureError, verify_entry_signature

__all__ = [
    "CatalogPullError",
    "PullResult",
    "lineage_job",
    "lineage_run_id",
    "materialize_yaml",
    "pull_entry",
    "render_model_yaml",
    "target_path",
]

#: The OpenLineage job name every catalog pull is recorded under.
LINEAGE_JOB = "catalog-pull"
#: Required keys of the per-model YAML schema — what ``pipelines.model_loader`` reads first.
_REQUIRED_YAML_KEYS = ("name", "config_class", "task_type")
#: Explicit override of where a relative template path is resolved from (highest precedence).
_TEMPLATE_ROOT_ENV = "EXAMLOPS_CATALOG_TEMPLATE_DIR"


class CatalogPullError(RuntimeError):
    """A pull that cannot proceed — an unknown entry, a missing project, an occupied target."""


@dataclass(frozen=True)
class PullResult:
    """What a pull did (or, under ``dry_run``, what it would have done)."""

    entry: str
    catalog_version: int
    entry_hash: str
    project: str
    model_name: str
    path: str
    rendered: str
    trust_tier: str
    unsigned_source: bool
    evaluated: bool
    dry_run: bool
    lineage_run_id: str
    lineage_edge: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "catalog_version": self.catalog_version,
            "entry_hash": self.entry_hash,
            "project": self.project,
            "model_name": self.model_name,
            "path": self.path,
            "model_yaml": self.rendered,
            "trust_tier": self.trust_tier,
            "unsigned_source": self.unsigned_source,
            "evaluated": self.evaluated,
            "dry_run": self.dry_run,
            "lineage_run_id": self.lineage_run_id,
            "lineage_edge": self.lineage_edge,
            # Said in the payload, not left to be inferred (ADR 0158 decision 4): a pull is
            # pre-training discovery, so nothing here is a registry version.
            "trained": False,
            "served": False,
            "registry_version_created": False,
        }


def lineage_job() -> str:
    return LINEAGE_JOB


def lineage_run_id(entry: CatalogEntry, model_name: str) -> str:
    """Deterministic per (entry version, model): re-pulling the same pair is one lineage run."""
    return f"catalog-pull-{entry.name}-{entry.catalog_version}-{model_name}"


def catalog_entry_uri(entry: CatalogEntry) -> str:
    """``examlops://catalog_entry/<name>@<catalog_version>`` (ADR 0158 decision 2)."""
    return f"examlops://catalog_entry/{entry.ref}"


# ── rendering ─────────────────────────────────────────────────────────────────────────────

#: The default per-model YAML a bare ``base_model`` entry materializes into. Deliberately
#: ``enabled: false`` with an empty dataset list: nothing has been trained, so nothing should be
#: picked up by a scheduled pipeline until an operator has filled it in.
_BASE_MODEL_TEMPLATE = """\
# Materialized by `exa catalog pull` from catalog entry ${catalog_entry} (${entry_hash}).
# A catalog entry is a STARTING POINT, not a registry version: nothing here has been trained or
# served. Edit the datasets and lifecycle gates, then `exa pipeline run --model ${model_name}`.
name: ${model_name}
model_class: ${model_class}
config_class: ${config_class}
task_type: ${task_type}
framework: ${framework}
enabled: false
project: ${project}

model: {}

datasets: []

lifecycle: []

serving:
  model_id: ${model_id}
  aliases: [Staging]

inference: {}
"""


def _template_roots() -> list[Path]:
    """Where a relative recipe-template path is resolved from, in precedence order.

    1. ``EXAMLOPS_CATALOG_TEMPLATE_DIR`` — the explicit override, for a curator trying a template
       out before it is committed to a pack.
    2. **The active use-case pack** — a pack's own ``catalog/templates/``, wherever that pack is:
       a checkout, ``$EXAMLOPS_DATA_DIR/usecase`` (ADR 0128), or a pip-installed pack registered
       under ``examlops.usecase_packs``. Resolved through the published pack seam, so the catalog
       learns nothing about pack layout that the rest of the platform does not already know.
    3. **``<site config dir>/catalog``** (ADR 0128) — templates a *site* authors for itself rather
       than for a pack, kept in the instance-data root so they survive an upgrade and are captured
       by the config backup tier, exactly like ``clusters.yaml`` and ``policy.yaml``.

    Deliberately *not* the repository root or the process's working directory: a published wheel
    or image has no checkout around it, and a template found by walking out of the package would
    work on the one machine that has one (ADR 0129 §8).
    """
    from examlops.lifecycle.datadir import config_dir, usecase_pack_dir

    roots: list[Path] = []
    configured = os.getenv(_TEMPLATE_ROOT_ENV, "").strip()
    if configured:
        roots.append(Path(configured).expanduser())
    pack, _source = usecase_pack_dir()
    if pack is not None:
        roots.append(pack)
    roots.append(config_dir() / "catalog")
    return [root for i, root in enumerate(roots) if root not in roots[:i]]


def _read_template(relative: str) -> str:
    candidate = Path(relative)
    if candidate.is_absolute() and candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    for root in _template_roots():
        path = root / relative
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise CatalogPullError(
        f"recipe template {relative!r} was not found (looked in "
        f"{', '.join(str(r) for r in _template_roots())}). A recipe template is pack content: put "
        f"it in the active use-case pack (its path is pack-relative), in the site's own "
        f"<config dir>/catalog, or point {_TEMPLATE_ROOT_ENV} at the directory holding it"
    )


def _substitutions(entry: CatalogEntry, model_name: str, project: str) -> dict[str, str]:
    defaults = entry.defaults or {}
    return {
        "model_name": model_name,
        "model_id": str(defaults.get("model_id") or model_name).lower(),
        "project": project,
        "catalog_entry": entry.ref,
        "catalog_version": str(entry.catalog_version),
        "entry_hash": entry.entry_hash,
        "license": entry.license,
        "source_ref": entry.source_ref,
        "trust_tier": entry.trust_tier,
        "resource_hint": entry.resource_hint or "",
        "model_class": str(defaults.get("model_class") or model_name),
        "config_class": str(
            defaults.get("config_class") or f"{model_name.lower()}_config.{model_name}Configuration"
        ),
        "task_type": str(defaults.get("task_type") or "regression"),
        "framework": str(defaults.get("framework") or "sklearn"),
    }


def render_model_yaml(entry: CatalogEntry, model_name: str, project: str) -> str:
    """The per-model YAML this entry materializes into — pure, writes nothing.

    A ``recipe`` entry renders its curated template; a bare ``base_model`` renders the default
    skeleton above. In both cases the entry's ``recipe.defaults`` are merged over the result, and
    the identity fields (``name``, ``project``, ``serving.model_id``) are then re-asserted so a
    template can never mis-name the model it was pulled as.
    """
    source = (
        _read_template(entry.model_yaml_template)
        if entry.model_yaml_template
        else _BASE_MODEL_TEMPLATE
    )
    rendered = Template(source).safe_substitute(_substitutions(entry, model_name, project))
    doc = yaml.safe_load(rendered)
    if not isinstance(doc, dict):
        raise CatalogPullError(
            f"catalog entry {entry.ref} rendered something that is not a per-model YAML mapping"
        )
    for key, value in (entry.defaults or {}).items():
        doc[key] = value
    doc["name"] = model_name
    doc["project"] = project
    serving = dict(doc.get("serving") or {})
    serving.setdefault("aliases", ["Staging"])
    serving["model_id"] = model_name.lower()
    doc["serving"] = serving
    missing = [k for k in _REQUIRED_YAML_KEYS if not doc.get(k)]
    if missing:
        raise CatalogPullError(
            f"catalog entry {entry.ref} would render an invalid per-model YAML — missing "
            f"{', '.join(missing)}. Fix the entry's recipe template or its recipe.defaults."
        )
    header = (
        f"# Materialized by `exa catalog pull` from catalog entry {entry.ref}\n"
        f"# entry_hash: {entry.entry_hash}\n"
        f"# source: {entry.source_kind}:{entry.source_ref}  license: {entry.license}\n"
        f"# trust_tier: {entry.trust_tier}\n"
        "# Nothing has been trained or served: a catalog entry is a starting point, not a\n"
        "# registry version (ADR 0158 decision 4).\n"
    )
    if entry.trust_tier != "T1_signed":
        header += (
            "# WARNING: unsigned source — this definition came from a registered but UNSIGNED\n"
            "# catalog entry. Review it before training or serving anything from it.\n"
        )
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def target_path(model_name: str, models_dir: Path | None = None) -> Path:
    """Where a pulled model YAML lands: the active use-case pack's ``models/`` directory."""
    if models_dir is None:
        from examlops import usecase

        models_dir = usecase.models_dir()
    return Path(models_dir) / f"{model_name.lower()}.yaml"


def materialize_yaml(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


# ── the pull ──────────────────────────────────────────────────────────────────────────────


def pull_entry(
    entry: CatalogEntry,
    project: str,
    *,
    model_name: str | None = None,
    actor: str | None = None,
    dry_run: bool = False,
    models_dir: Path | None = None,
) -> PullResult:
    """Materialize ``entry`` into ``project``. Never trains, never serves, never promotes.

    Under ``dry_run`` nothing at all is written — no file, no membership row, no pull record, no
    lineage event — which is the same contract ``exa pipeline promote --dry-run`` already holds.
    """
    from examlops.data.projects import assign_resource_to_project, get_project

    name = model_name or entry.name.replace(".", "-")
    path = target_path(name, models_dir)
    rendered = render_model_yaml(entry, name, project)
    result = PullResult(
        entry=entry.name,
        catalog_version=entry.catalog_version,
        entry_hash=entry.entry_hash,
        project=project,
        model_name=name,
        path=str(path),
        rendered=rendered,
        trust_tier=entry.trust_tier,
        unsigned_source=not entry.signed,
        evaluated=entry.evaluated,
        dry_run=dry_run,
        lineage_run_id=lineage_run_id(entry, name),
        lineage_edge=f"{catalog_entry_uri(entry)} -> examlops://model/{name}",
    )
    if get_project(project) is None:
        raise CatalogPullError(
            f"project {project!r} does not exist — create it first (`exa project create "
            f"{project}`); a catalog entry is always pulled INTO a project (ADR 0086)"
        )
    # ADR 0158 decision 1's verify-before-load-shaped gate. Runs even under dry_run — a preview
    # that omits the one check that can refuse the real pull is not an accurate preview — and only
    # for T1_signed entries: an unsigned one has nothing to verify (already flagged via
    # `unsigned_source` above) and an unpinned source never became an entry at all (decision 3).
    if entry.trust_tier == "T1_signed":
        try:
            verify_entry_signature(entry.ref)
        except CatalogSignatureError as exc:
            raise CatalogPullError(
                f"{exc} — a catalog entry claiming to be signed must actually verify, or a pull "
                "would be trusting a false signal (ADR 0158 decision 3)"
            ) from exc
    if dry_run:
        return result
    if path.exists():
        raise CatalogPullError(
            f"{path} already exists — a pull never overwrites a model definition. Pull under "
            f"another name with --as, or remove the file first."
        )

    materialize_yaml(path, rendered)
    assign_resource_to_project(project, "model", name, added_by=actor)
    from examlops.data.catalog import record_pull

    record_pull(
        entry.name,
        entry.catalog_version,
        entry.entry_hash,
        project,
        name,
        actor=actor,
    )
    _emit_lineage(entry, name, project)
    return result


def _emit_lineage(entry: CatalogEntry, model_name: str, project: str) -> None:
    """One ``catalog_entry -> model`` edge through the platform's only lineage system."""
    from examlops.lineage import Node, emit_lineage

    emit_lineage(
        "COMPLETE",
        LINEAGE_JOB,
        lineage_run_id(entry, model_name),
        inputs=[Node(name=entry.ref, type="catalog_entry")],
        outputs=[Node(name=model_name, type="model")],
        facets={
            "catalog_entry": entry.ref,
            "entry_hash": entry.entry_hash,
            "trust_tier": entry.trust_tier,
            "license": entry.license,
            "project": project,
        },
        model=model_name,
    )
