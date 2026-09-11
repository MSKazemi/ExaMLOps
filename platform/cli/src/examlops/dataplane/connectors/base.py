"""The connector contract (ADR 0130 §5)."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

from examlops.dataplane.types import (
    Limits,
    Probe,
    ResolvedConnection,
    TableBatch,
    TableInfo,
    Watermark,
)


@runtime_checkable
class Connector(Protocol):
    kind: str
    connection_kinds: tuple[str, ...]
    extra: str | None
    connection_required: bool
    supports_incremental: bool

    def available(self) -> tuple[bool, str]: ...

    def validate_spec(self, spec: dict[str, Any]) -> list[str]: ...

    def probe(
        self, conn: ResolvedConnection | None, spec: dict[str, Any] | None = None
    ) -> Probe: ...

    def discover(
        self, conn: ResolvedConnection | None, spec: dict[str, Any]
    ) -> list[TableInfo]: ...

    def read(
        self,
        conn: ResolvedConnection | None,
        spec: dict[str, Any],
        since: Watermark | None,
        limits: Limits,
    ) -> Iterator[TableBatch]: ...


class BaseConnector:
    """Defaults for the boring parts; subclasses implement ``probe`` and ``read``."""

    kind: str = ""
    connection_kinds: tuple[str, ...] = ()
    extra: str | None = None
    requires: tuple[str, ...] = ()
    required_spec: tuple[str, ...] = ()
    connection_required: bool = True
    supports_incremental: bool = False

    def available(self) -> tuple[bool, str]:
        missing = [m for m in self.requires if importlib.util.find_spec(m) is None]
        if not missing:
            return True, ""
        hint = f" — pip install 'examlops[{self.extra}]'" if self.extra else ""
        return False, f"missing {', '.join(missing)}{hint}"

    def validate_spec(self, spec: dict[str, Any]) -> list[str]:
        return [
            f"spec.{k} is required" for k in self.required_spec if spec.get(k) in (None, "", [])
        ]

    def probe(self, conn: ResolvedConnection | None, spec: dict[str, Any] | None = None) -> Probe:
        raise NotImplementedError

    def discover(self, conn: ResolvedConnection | None, spec: dict[str, Any]) -> list[TableInfo]:
        return []

    def read(
        self,
        conn: ResolvedConnection | None,
        spec: dict[str, Any],
        since: Watermark | None,
        limits: Limits,
    ) -> Iterator[TableBatch]:
        raise NotImplementedError
