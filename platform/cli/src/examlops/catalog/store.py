"""Publishing and reading catalog entries (ADR 0158 Phase 1).

The trust gate lives here, at **publish** time, because that is where ADR 0158 decision 3 puts it:
an ``unpinned`` source never becomes a catalog entry at all, so the only question a pull ever has
to ask is signed-versus-unsigned. Signing is :mod:`examlops.supplychain` and nothing else — the
same Ed25519/HMAC mechanism a model version is signed with, over the entry's own canonical
manifest bytes. There is deliberately no second signing mechanism here (ADR 0158 "Alternatives").

Reading is a plain listing: the catalog answers "what could I start from?", so a caller filters by
kind, license, trust tier or "has an eval summary" — never by alias, stage or metric, which are
registry concepts an entry does not have (decision 4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from examlops.catalog.manifest import (
    CatalogEntry,
    CatalogEntryError,
    canonical_json,
    entry_hash_of,
    normalize,
)
from examlops.data import catalog as _store

__all__ = [
    "get_entry",
    "latest_version",
    "list_entries",
    "list_pulls",
    "publish_entry",
    "resolve_ref",
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def publish_entry(
    doc: dict[str, Any],
    *,
    actor: str | None = None,
    sign: bool = False,
) -> tuple[bool, CatalogEntry]:
    """Validate, optionally sign, and store ``doc``. Returns ``(created, entry)``.

    ``created`` is ``False`` when the same content was already published under this name — a
    duplicate publish is idempotent and allocates no new ``catalog_version`` (spec §3 Phase 1).

    Raises :class:`~examlops.catalog.manifest.CatalogEntryError` for an invalid document, for an
    absent or unknown license, and for an unpinned ``pinned_uri`` source. ``sign=True`` asks
    :func:`examlops.supplychain.sign_or_explain` for a signature over the canonical manifest; if
    no signing key is configured the entry is still published, as **T1_unsigned** with the reason
    surfaced — never as an equal-trust entry that merely looks signed.
    """
    body = dict(doc)
    body.pop("entry_hash", None)
    body.pop("catalog_version", None)  # allocated by the store, never taken from the file
    body["published_by"] = actor
    body["published_at"] = _now()
    # The tier is decided here, not read from the file: a curator may not self-declare a signature
    # this platform did not make. Validation then re-checks the pair for consistency.
    provenance = dict(body.get("provenance") or {})
    provenance.pop("supplychain_ref", None)
    provenance["trust_tier"] = "T1_unsigned"
    body["provenance"] = provenance

    entry = normalize(body)
    signature: str | None = None
    if sign:
        from examlops import supplychain

        payload = canonical_json({k: v for k, v in entry.items() if k != "provenance"})
        signature, algo = supplychain.sign_or_explain(payload, subject=f"catalog:{entry['name']}")
        if signature is not None:
            entry["provenance"] = {
                "trust_tier": "T1_signed",
                "supplychain_ref": f"{algo}:{signature}",
            }
            entry = normalize(entry)

    entry_hash = entry_hash_of(entry)
    created, row = _store.insert_entry(
        entry["name"],
        entry_hash,
        canonical_json(entry),
        kind=entry["kind"],
        source_kind=entry["source"]["kind"],
        source_ref=entry["source"]["ref"],
        license_id=entry["license"],
        trust_tier=entry["provenance"]["trust_tier"],
        resource_hint=entry["resource_hint"],
        eval_suite=entry["eval_summary_ref"]["suite"],
        eval_model_version=entry["eval_summary_ref"]["model_version"],
        supplychain_ref=entry["provenance"]["supplychain_ref"],
        model_yaml_template=entry["recipe"]["model_yaml_template"],
        description=entry["description"],
        published_by=actor,
    )
    return created, CatalogEntry.from_row(row)


def resolve_ref(ref: str) -> tuple[str, int | None]:
    """Split ``name[@version]`` into ``(name, version|None)``.

    Raises :class:`CatalogEntryError` on a non-integer version rather than silently pulling the
    latest — "``@main``" must not quietly become "whatever is newest".
    """
    name, _, version = str(ref).partition("@")
    if not version:
        return name, None
    if not version.isdigit():
        raise CatalogEntryError(
            [f"{ref!r}: a catalog version is an integer (e.g. '{name}@1'), not {version!r}"]
        )
    return name, int(version)


def get_entry(ref: str) -> CatalogEntry | None:
    """One entry by ``name[@version]`` (default: its newest version), or ``None``."""
    name, version = resolve_ref(ref)
    row = _store.get_entry_row(name, version) if version else _store.latest_entry_row(name)
    return CatalogEntry.from_row(row) if row else None


def latest_version(name: str) -> int | None:
    row = _store.latest_entry_row(name)
    return int(row["catalog_version"]) if row else None


def list_entries(
    *,
    kind: str | None = None,
    license_id: str | None = None,
    trust_tier: str | None = None,
    evaluated_only: bool = False,
    all_versions: bool = False,
) -> list[CatalogEntry]:
    """Entries by name (newest version of each, unless ``all_versions``), filtered."""
    entries = [
        CatalogEntry.from_row(r) for r in _store.list_entry_rows(latest_only=not all_versions)
    ]
    if kind:
        entries = [e for e in entries if e.kind == kind]
    if license_id:
        entries = [e for e in entries if e.license.lower() == license_id.lower()]
    if trust_tier:
        entries = [e for e in entries if e.trust_tier == trust_tier]
    if evaluated_only:
        entries = [e for e in entries if e.evaluated]
    return entries


def list_pulls(
    *, project: str | None = None, entry: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """The pull records — the only thread connecting a catalog entry to a project's model."""
    return _store.list_pull_rows(project=project, entry=entry, limit=limit)
