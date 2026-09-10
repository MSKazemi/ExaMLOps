"""ANN index configuration for a vector collection (ADR 0020 clause 2).

A collection declares which approximate-nearest-neighbour index serves it and with which
parameters. The configuration is validated here, once, against the limits pgvector enforces, so a
bad value fails at ``create`` with a readable message instead of as a Postgres error in the middle
of an index build.

The three types:

- ``flat`` — no ANN index. Every search is an exact scan: recall 1.0, cost O(N·d). Right for small
  collections and the only mode the dependency-free SQLite store has.
- ``hnsw`` — Hierarchical Navigable Small World graph (Malkov & Yashunin, IEEE TPAMI 2020,
  arXiv:1603.09320). Best recall/latency trade-off; build is slower and memory-heavier than IVF.
  ``m`` is the per-node link count, ``ef_construction`` the build-time candidate list, and
  ``ef_search`` the query-time candidate list: raising it buys recall with latency.
- ``ivfflat`` — inverted file with flat lists (Jégou et al., IEEE TPAMI 2011). Rows are clustered
  into ``lists`` centroids; a query scans the ``probes`` nearest lists. Builds fast and small, but
  the centroids are trained on the rows present **when the index is built**, so build it after
  loading (``exa vector reindex``); an IVF index built on an empty table has meaningless lists.

Defaults are pgvector's own (``m=16``, ``ef_construction=64``, ``ef_search=40``; ``probes=1``),
except ``lists``, which pgvector leaves to the operator and whose documented rule of thumb is
``rows / 1000`` up to 1M rows — 100 is the value for the ~100k-row collections this platform holds.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

INDEX_TYPES = ("flat", "hnsw", "ivfflat")

# pgvector limits (https://github.com/pgvector/pgvector, "HNSW" / "IVFFlat" sections). Checked
# here rather than left to Postgres so the SQLite store — which builds no index — still refuses a
# configuration that would fail the day the same collection moves to pgvector.
_MAX_INDEXED_DIM = 2000  # vector type; halfvec allows 4000 but is not used here
_HNSW_M = (2, 100)
_HNSW_EF_CONSTRUCTION = (4, 1000)
_EF_SEARCH = (1, 1000)
_IVF_LISTS = (1, 32768)

_DEFAULTS: dict[str, dict[str, int]] = {
    "flat": {},
    "hnsw": {"m": 16, "ef_construction": 64, "ef_search": 40},
    "ivfflat": {"lists": 100, "probes": 1},
}
_ALLOWED: dict[str, frozenset[str]] = {k: frozenset(v) for k, v in _DEFAULTS.items()}


class IndexConfigError(ValueError):
    """An index configuration pgvector would refuse."""


@dataclass(frozen=True)
class IndexConfig:
    """Validated ANN index configuration. Build with :meth:`build`, not the constructor."""

    type: str = "flat"
    m: int | None = None
    ef_construction: int | None = None
    ef_search: int | None = None
    lists: int | None = None
    probes: int | None = None

    @classmethod
    def build(cls, type: str = "flat", dim: int | None = None, **params: Any) -> IndexConfig:
        """Validate ``params`` for ``type`` and fill pgvector defaults for the rest.

        ``None`` values are treated as "not given", so CLI options that default to ``None`` can be
        passed straight through. A parameter that belongs to another index type is an error, not
        silently dropped: ``--lists`` on an HNSW collection is a misunderstanding worth surfacing.
        """
        kind = (type or "flat").lower()
        if kind not in INDEX_TYPES:
            raise IndexConfigError(f"index type '{type}' not in {INDEX_TYPES}")
        given = {k: v for k, v in params.items() if v is not None}
        stray = set(given) - _ALLOWED[kind]
        if stray:
            raise IndexConfigError(
                f"parameter(s) {sorted(stray)} do not apply to a '{kind}' index "
                f"(allowed: {sorted(_ALLOWED[kind]) or 'none'})"
            )
        merged = {**_DEFAULTS[kind], **{k: int(v) for k, v in given.items()}}
        cfg = cls(type=kind, **merged)
        cfg._validate(dim)
        return cfg

    def _validate(self, dim: int | None) -> None:
        def _range(name: str, value: int | None, lo: int, hi: int) -> None:
            if value is not None and not lo <= value <= hi:
                raise IndexConfigError(f"{name}={value} outside [{lo}, {hi}]")

        if self.type != "flat" and dim is not None and dim > _MAX_INDEXED_DIM:
            raise IndexConfigError(
                f"a {self.type} index supports at most {_MAX_INDEXED_DIM} dimensions "
                f"(collection has {dim}); use a flat collection or reduce the dimension"
            )
        if self.type == "hnsw":
            _range("m", self.m, *_HNSW_M)
            _range("ef_construction", self.ef_construction, *_HNSW_EF_CONSTRUCTION)
            _range("ef_search", self.ef_search, *_EF_SEARCH)
            # pgvector refuses this at CREATE INDEX: the build-time candidate list must be able
            # to hold both directions of every link a node keeps.
            if self.m is not None and self.ef_construction is not None:
                if self.ef_construction < 2 * self.m:
                    raise IndexConfigError(
                        f"ef_construction={self.ef_construction} must be >= 2*m ({2 * self.m})"
                    )
        elif self.type == "ivfflat":
            _range("lists", self.lists, *_IVF_LISTS)
            if self.probes is not None and self.lists is not None:
                _range("probes", self.probes, 1, self.lists)

    @property
    def params(self) -> dict[str, int]:
        """The parameters that apply to this index type (no ``None`` placeholders)."""
        return {k: v for k, v in asdict(self).items() if k != "type" and v is not None}

    def to_json(self) -> str:
        return json.dumps(self.params, sort_keys=True)

    @classmethod
    def from_row(cls, index_type: str | None, index_params: str | None) -> IndexConfig:
        """Rehydrate a stored configuration. A row written before these columns existed is flat.

        Stored values are trusted to have been validated at write time, but they are re-validated
        anyway: a hand-edited row that pgvector would refuse should fail here, readably.
        """
        if not index_type:
            return cls()
        try:
            params = json.loads(index_params) if index_params else {}
        except ValueError:
            params = {}
        return cls.build(index_type, **params)

    def describe(self) -> str:
        if self.type == "flat":
            return "flat (exact scan)"
        inner = ", ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.type} ({inner})"
