"""The ``CatalogEntry`` manifest (ADR 0158 decision 1): typed, canonical, content-addressed.

A catalog entry answers **"what could I start from?"** — a curated, provenance-tracked *model
definition* an operator may pull into a project. It is not a registry version: it has no training
run, no metrics, no alias, and it is never served (ADR 0158 decision 4). Everything here is pure —
no database, no network, no filesystem — mirroring :mod:`examlops.agent_versions.manifest`, the
closest structural analogue in this repo.

**What ``entry_hash`` covers.** ADR 0158 writes the hash as "hash of every field *below*", and the
fields above it are ``name`` and ``catalog_version`` — the entry's identity coordinates, not its
content. That reading is the one that works: a hash over the coordinates could never be stable
across a republish, so a duplicate publish could not be recognised as one. The hash therefore
covers the *definition* (source, kind, license, resource hint, eval pointer, trust tier, recipe,
description) and excludes:

* ``name`` / ``catalog_version`` — the coordinates the hash is stored under;
* ``entry_hash`` itself — derived, never inside what it hashes (as ``version_id_of`` does);
* ``published_by`` / ``published_at`` — the publishing *act*, not the thing published;
* ``provenance.supplychain_ref`` — a signature is computed *over* the identity, so it cannot be
  *in* it. ``provenance.trust_tier`` **is** hashed: signed and unsigned are different entries.

Validation is strict on purpose. An unknown field is refused (a typo is not a free comment), an
absent or unknown ``license`` is refused rather than defaulted, and a ``pinned_uri`` source whose
reference is not demonstrably immutable is refused outright — ADR 0158 decision 3's ``unpinned``
tier never reaches storage, so the pull-time question is only signed-versus-unsigned.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "KINDS",
    "LICENSE_PLACEHOLDERS",
    "RESOURCE_HINTS",
    "SCHEMA_VERSION",
    "SOURCE_KINDS",
    "TRUST_TIERS",
    "TRUST_UNPINNED",
    "CatalogEntry",
    "CatalogEntryError",
    "canonical_json",
    "entry_hash_of",
    "normalize",
    "pinned_ref_problem",
]

SCHEMA_VERSION = 1

#: A bare pretrained base model, versus a base plus a validated training/serving config.
KINDS = ("base_model", "recipe")
#: How the entry's upstream is reached: a dataplane ``(connection, spec)`` or a pinned URI.
SOURCE_KINDS = ("dataplane", "pinned_uri")
#: The two tiers an entry may carry. ``unpinned`` is refused at publish and is never stored.
TRUST_TIERS = ("T1_signed", "T1_unsigned")
TRUST_UNPINNED = "unpinned"
#: The small curated vocabulary until ADR 0157's profile names are wired in (spec §1.1).
RESOURCE_HINTS = ("cpu-small", "cpu-large", "gpu-1x-24gb", "gpu-1x-80gb", "gpu-4x-80gb")
#: Spellings that mean "we did not look" — refused, never accepted as a license (decision 1).
LICENSE_PLACEHOLDERS = ("unknown", "none", "n/a", "na", "noassertion", "todo", "tbd")

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUITE = re.compile(r"^[A-Za-z0-9._-]+@[0-9]+$")
_SPDX = re.compile(r"^[A-Za-z0-9.+-]{1,64}(?: (?:WITH|OR|AND) [A-Za-z0-9.+-]{1,64})*$")
#: ``<connection>/<spec>`` — the pair ``examlops.dataplane.connectors`` resolves.
_DATAPLANE_REF = re.compile(r"^[A-Za-z0-9._-]{1,128}/[A-Za-z0-9._-]{1,128}$")

_REQUIRED = ("name", "kind", "source", "license")
_OPTIONAL = (
    "schema_version",
    "catalog_version",
    "resource_hint",
    "eval_summary_ref",
    "provenance",
    "recipe",
    "description",
    "published_by",
    "published_at",
)
#: Derived, and so never part of what the hash covers — see the module docstring.
_NOT_HASHED = ("name", "catalog_version", "entry_hash", "published_by", "published_at")

# ── immutability of a pinned_uri source (decision 3) ──────────────────────────────────────

#: Any one of these makes a reference immutable. A git commit SHA (40 or 64 hex), an OCI/content
#: digest, or a Zenodo *versioned* record id — the three shapes this platform actually pulls from.
_IMMUTABLE_POINTERS = (
    re.compile(r"@sha256:[0-9a-f]{64}(?:$|[/?#])"),
    re.compile(r"(?:^|[@/=])[0-9a-f]{40}(?:$|[/?#])"),
    re.compile(r"(?:^|[@/=])[0-9a-f]{64}(?:$|[/?#])"),
    re.compile(r"/(?:record|records)/[0-9]{4,}(?:$|[/?#])"),
    re.compile(r"zenodo\.[0-9]{4,}(?:$|[/?#])"),
)
#: Spellings that are floating by construction — refused even if something hex-looking follows.
_FLOATING = re.compile(r"[@:/](?:latest|main|master|head|dev|nightly|stable)(?:$|[/?#])", re.I)
_SCHEMES = ("http://", "https://", "s3://", "oci://", "gs://", "abfs://")


def pinned_ref_problem(ref: str) -> str | None:
    """Why ``ref`` is **not** an immutable pointer, or ``None`` when it is one.

    ADR 0158 decision 3: an unpinned source never becomes a catalog entry at all. The check is a
    named reason rather than a boolean so the refusal can say what would fix it.
    """
    if not isinstance(ref, str) or not ref.strip():
        return "source.ref: must be a non-empty string"
    value = ref.strip()
    if not value.lower().startswith(_SCHEMES):
        return f"source.ref: {value!r} is not a URI (expected one of {', '.join(_SCHEMES)})"
    if _FLOATING.search(value):
        return (
            f"{TRUST_UNPINNED}: source.ref {value!r} names a moving reference (a branch or a "
            "'latest' tag). Pin it to a commit SHA or a content digest — the catalog only ever "
            "offers immutable refs."
        )
    if not any(p.search(value) for p in _IMMUTABLE_POINTERS):
        return (
            f"{TRUST_UNPINNED}: source.ref {value!r} embeds no immutable pointer. Append a git "
            "commit SHA (…@<40-hex>), a content digest (…@sha256:<64-hex>) or use a versioned "
            "record id — an unpinned source is refused at publish time, not flagged at pull time."
        )
    return None


# ── errors + canonical form ───────────────────────────────────────────────────────────────


class CatalogEntryError(ValueError):
    """The document is not a valid ``CatalogEntry``; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def canonical_json(doc: Any) -> str:
    """Byte-stable JSON: sorted keys, no whitespace, UTF-8 kept, NaN/Infinity refused."""
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def entry_hash_of(doc: dict[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical JSON of the entry's *definition*.

    See the module docstring for exactly which fields are excluded and why.
    """
    body = {k: v for k, v in doc.items() if k not in _NOT_HASHED}
    prov = body.get("provenance")
    if isinstance(prov, dict):
        body["provenance"] = {k: v for k, v in prov.items() if k != "supplychain_ref"}
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# ── validation ────────────────────────────────────────────────────────────────────────────


def _unknown(where: str, obj: dict[str, Any], allowed: set[str], problems: list[str]) -> None:
    for key in sorted(set(obj) - allowed):
        problems.append(f"{where}.{key}: unknown field")


def _str(
    where: str, v: Any, problems: list[str], *, pattern: re.Pattern[str] | None = None
) -> bool:
    if not isinstance(v, str) or not v.strip():
        problems.append(f"{where}: must be a non-empty string")
        return False
    if pattern is not None and not pattern.match(v.strip()):
        problems.append(f"{where}: {v!r} does not match the required form")
        return False
    return True


def _check_source(v: Any, problems: list[str]) -> None:
    if not isinstance(v, dict):
        problems.append("source: must be an object with 'kind' and 'ref'")
        return
    _unknown("source", v, {"kind", "ref"}, problems)
    kind = v.get("kind")
    if kind not in SOURCE_KINDS:
        problems.append(f"source.kind: required, one of {', '.join(SOURCE_KINDS)}")
    if not _str("source.ref", v.get("ref"), problems):
        return
    ref = str(v["ref"]).strip()
    if kind == "pinned_uri":
        problem = pinned_ref_problem(ref)
        if problem:
            problems.append(problem)
    elif kind == "dataplane" and not _DATAPLANE_REF.match(ref):
        problems.append(
            f"source.ref: {ref!r} must be '<connection>/<spec>' for a dataplane source — the pair "
            "examlops.dataplane.connectors resolves (its egress allow-list is reused verbatim)"
        )


def _check_license(v: Any, problems: list[str]) -> None:
    if not _str("license", v, problems):
        return
    value = str(v).strip()
    if value.lower() in LICENSE_PLACEHOLDERS:
        problems.append(
            f"license: {value!r} is not a license — an unknown or absent license is refused at "
            "publish time, never silently defaulted (ADR 0158 decision 1). Give an SPDX id."
        )
    elif not _SPDX.match(value):
        problems.append(f"license: {value!r} is not an SPDX-shaped identifier")


def _check_eval_ref(v: Any, problems: list[str]) -> None:
    if v is None:
        return
    if not isinstance(v, dict):
        problems.append("eval_summary_ref: must be an object with 'suite' and 'model_version'")
        return
    _unknown("eval_summary_ref", v, {"suite", "model_version"}, problems)
    if v.get("suite") is not None:
        _str("eval_summary_ref.suite", v["suite"], problems, pattern=_SUITE)
    if v.get("model_version") is not None:
        _str("eval_summary_ref.model_version", v["model_version"], problems)


def _check_provenance(v: Any, problems: list[str]) -> None:
    if v is None:
        return
    if not isinstance(v, dict):
        problems.append("provenance: must be an object")
        return
    _unknown("provenance", v, {"supplychain_ref", "trust_tier"}, problems)
    tier = v.get("trust_tier")
    if tier is not None and tier not in TRUST_TIERS:
        if tier == TRUST_UNPINNED:
            problems.append(
                "provenance.trust_tier: 'unpinned' is refused at publish time and never stored "
                "(ADR 0158 decision 3) — pin the source instead"
            )
        else:
            problems.append(
                f"provenance.trust_tier: must be one of {', '.join(TRUST_TIERS)}, not {tier!r}"
            )
    ref = v.get("supplychain_ref")
    if ref is not None:
        _str("provenance.supplychain_ref", ref, problems)
    if tier == "T1_signed" and not ref:
        problems.append(
            "provenance.supplychain_ref: required when trust_tier is T1_signed — a signed entry "
            "names the examlops.supplychain signature that signed it (there is no second "
            "signing mechanism)"
        )
    if tier == "T1_unsigned" and ref:
        problems.append("provenance.supplychain_ref: must be absent when trust_tier is T1_unsigned")


def _check_recipe(v: Any, kind: Any, problems: list[str]) -> None:
    if v is None:
        if kind == "recipe":
            problems.append(
                "recipe.model_yaml_template: required when kind is 'recipe' (a recipe IS the "
                "validated config; without a template there is nothing to render on pull)"
            )
        return
    if not isinstance(v, dict):
        problems.append("recipe: must be an object")
        return
    _unknown("recipe", v, {"model_yaml_template", "defaults"}, problems)
    template = v.get("model_yaml_template")
    if template is not None:
        if _str("recipe.model_yaml_template", template, problems) and (
            str(template).startswith("/") or ".." in str(template).split("/")
        ):
            problems.append(
                "recipe.model_yaml_template: must be a relative path without '..' segments — it "
                "is resolved inside the active use-case pack (or the site's own catalog template "
                "directory), never against a repository checkout"
            )
    elif kind == "recipe":
        problems.append("recipe.model_yaml_template: required when kind is 'recipe'")
    defaults = v.get("defaults")
    if defaults is not None and not isinstance(defaults, dict):
        problems.append("recipe.defaults: must be an object of per-model-YAML overrides")


def normalize(doc: Any) -> dict[str, Any]:
    """Validate ``doc`` and return the canonical entry (every optional block filled in).

    Raises :class:`CatalogEntryError` listing **every** problem, not just the first — a curator
    editing an entry YAML should see the whole list in one run.
    """
    problems: list[str] = []
    if not isinstance(doc, dict):
        raise CatalogEntryError(["entry: must be a mapping"])
    for key in _REQUIRED:
        if key not in doc:
            problems.append(f"{key}: required field is missing")
    for key in sorted(set(doc) - set(_REQUIRED) - set(_OPTIONAL) - {"entry_hash"}):
        problems.append(f"{key}: unknown field")
    if "schema_version" in doc and doc["schema_version"] != SCHEMA_VERSION:
        problems.append(f"schema_version: must be {SCHEMA_VERSION}")
    if "name" in doc:
        _str("name", doc["name"], problems, pattern=_NAME)
    kind = doc.get("kind")
    if kind not in KINDS:
        problems.append(f"kind: required, one of {', '.join(KINDS)}")
    if "source" in doc:
        _check_source(doc["source"], problems)
    if "license" in doc:
        _check_license(doc["license"], problems)
    hint = doc.get("resource_hint")
    if hint is not None and hint not in RESOURCE_HINTS:
        problems.append(f"resource_hint: must be one of {', '.join(RESOURCE_HINTS)}, not {hint!r}")
    _check_eval_ref(doc.get("eval_summary_ref"), problems)
    _check_provenance(doc.get("provenance"), problems)
    _check_recipe(doc.get("recipe"), kind, problems)
    if "description" in doc and doc["description"] is not None:
        _str("description", doc["description"], problems)
    if problems:
        raise CatalogEntryError(problems)

    out = json.loads(canonical_json(doc))  # deep copy; also refuses NaN and non-JSON values
    out.pop("entry_hash", None)  # derived, never stored inside the content it hashes
    out["schema_version"] = SCHEMA_VERSION
    out["name"] = str(out["name"]).strip()
    out["source"] = {"kind": out["source"]["kind"], "ref": str(out["source"]["ref"]).strip()}
    out["license"] = str(out["license"]).strip()
    out.setdefault("resource_hint", None)
    out.setdefault("description", "")
    ev = out.get("eval_summary_ref") or {}
    out["eval_summary_ref"] = {
        "suite": ev.get("suite"),
        "model_version": ev.get("model_version"),
    }
    prov = out.get("provenance") or {}
    out["provenance"] = {
        "trust_tier": prov.get("trust_tier") or "T1_unsigned",
        "supplychain_ref": prov.get("supplychain_ref"),
    }
    recipe = out.get("recipe") or {}
    out["recipe"] = {
        "model_yaml_template": recipe.get("model_yaml_template"),
        "defaults": recipe.get("defaults") or {},
    }
    return out


# ── the typed record ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CatalogEntry:
    """One immutable, content-addressed catalog entry (ADR 0158 decision 1).

    Frozen because a published entry is never rewritten: a correction publishes the next
    ``catalog_version``, exactly as ``agent_versions`` rows are insert-only.
    """

    name: str
    catalog_version: int
    entry_hash: str
    kind: str
    source_kind: str
    source_ref: str
    license: str
    trust_tier: str
    resource_hint: str | None = None
    eval_suite: str | None = None
    eval_model_version: str | None = None
    supplychain_ref: str | None = None
    model_yaml_template: str | None = None
    defaults: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    published_by: str | None = None
    published_at: str | None = None

    @property
    def ref(self) -> str:
        """``name@catalog_version`` — how an entry is addressed on the CLI and in lineage."""
        return f"{self.name}@{self.catalog_version}"

    @property
    def signed(self) -> bool:
        return self.trust_tier == "T1_signed"

    @property
    def evaluated(self) -> bool:
        """Whether the entry points at an eval summary at all.

        An entry with none is *allowed* but flagged wherever it is shown — the same discipline
        ADR 0111 applies to an uncalibrated judge: absence is named, never read as equivalence.
        """
        return bool(self.eval_suite)

    def manifest(self) -> dict[str, Any]:
        """The normalized document this entry was published from (replays ``entry_hash``)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "catalog_version": self.catalog_version,
            "kind": self.kind,
            "source": {"kind": self.source_kind, "ref": self.source_ref},
            "license": self.license,
            "resource_hint": self.resource_hint,
            "eval_summary_ref": {
                "suite": self.eval_suite,
                "model_version": self.eval_model_version,
            },
            "provenance": {
                "trust_tier": self.trust_tier,
                "supplychain_ref": self.supplychain_ref,
            },
            "recipe": {
                "model_yaml_template": self.model_yaml_template,
                "defaults": dict(self.defaults),
            },
            "description": self.description,
            "published_by": self.published_by,
            "published_at": self.published_at,
        }

    def to_dict(self) -> dict[str, Any]:
        """A flat, JSON-safe view — what ``exa catalog show --json`` prints."""
        doc = self.manifest()
        doc["entry_hash"] = self.entry_hash
        doc["ref"] = self.ref
        doc["evaluated"] = self.evaluated
        # The distinction ADR 0158 decision 4 exists to protect, said in the payload itself so a
        # reader (or an agent) never has to infer it from the shape.
        doc["is_registry_model"] = False
        return doc

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CatalogEntry:
        """Build an entry from a ``catalog_entries`` row (``manifest_json`` is authoritative)."""
        manifest = json.loads(row["manifest_json"])
        recipe = manifest.get("recipe") or {}
        return cls(
            name=row["name"],
            catalog_version=int(row["catalog_version"]),
            entry_hash=row["entry_hash"],
            kind=row["kind"],
            source_kind=row["source_kind"],
            source_ref=row["source_ref"],
            license=row["license"],
            trust_tier=row["trust_tier"],
            resource_hint=row["resource_hint"],
            eval_suite=row["eval_suite"],
            eval_model_version=row["eval_model_version"],
            supplychain_ref=row["supplychain_ref"],
            model_yaml_template=row["model_yaml_template"],
            defaults=dict(recipe.get("defaults") or {}),
            description=row["description"] or "",
            published_by=row["published_by"],
            published_at=str(row["published_at"]) if row["published_at"] is not None else None,
        )
