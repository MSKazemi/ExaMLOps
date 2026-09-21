"""The typed, versioned offline-inference job spec (ADR 0149 decisions 1 and 2).

Pure: nothing here touches a database, a registry or a filesystem. :meth:`OfflineJob.problems`
lists every reason a spec is invalid (not just the first), and :meth:`OfflineJob.from_dict` is
strict - an unknown key is an error, so a typo (``batchsize``) is never silently ignored.

A spec names *what* to run (a servable version), *over what* (a dataplane snapshot revision or a
local Parquet path), *where the output goes*, and the resources the run declares. The servable is
resolved to an immutable version and the input to a revision/digest at run time
(:mod:`examlops.offline.executor`); the spec hash that guards the idempotency key is taken over the
*resolved* spec, so a moved alias cannot silently change what a retried key means.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

KINDS = ("predictive", "generative", "agentic")
INPUT_TYPES = ("local", "dataplane")
OUTPUT_TYPES = ("local", "dataplane")
MAX_BATCH_SIZE = 1_000_000

_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,199}")
_REV_RE = re.compile(r"[0-9a-f]{64}")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}")

_TOP_KEYS = {
    "schema_version",
    "kind",
    "model",
    "version",
    "alias",
    "input",
    "output",
    "batch_size",
    "resources",
    "idempotency_key",
    "tenant",
    "project",
    "flexibility_s",
}
_INPUT_KEYS = {"type", "path", "source", "project", "revision", "table"}
_OUTPUT_KEYS = {"type", "path", "source", "project"}
_RESOURCE_KEYS = {"cpus", "gpus", "memory_gb"}


class OfflineSpecError(ValueError):
    """An invalid spec; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(problems))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class InputSpec:
    """``local``: a Parquet file or a directory of Parquet files. ``dataplane``: table ``table`` of
    snapshot ``revision`` (``latest`` or a full 64-hex id) of source ``source``."""

    type: str = "local"
    path: str | None = None
    source: str | None = None
    project: str = ""
    revision: str = "latest"
    table: str | None = None


@dataclass(frozen=True)
class OutputSpec:
    """``local``: a directory that receives ``<revision>/`` (content-addressed). ``dataplane``: a
    new snapshot of source ``source`` in the dataset store."""

    type: str = "local"
    path: str | None = None
    source: str | None = None
    project: str = ""


@dataclass(frozen=True)
class Resources:
    """Declared, not enforced: the run records them and prices its cost from them."""

    cpus: int = 0
    gpus: int = 0
    memory_gb: float = 0.0


@dataclass(frozen=True)
class OfflineJob:
    model: str
    idempotency_key: str
    input: InputSpec
    output: OutputSpec
    kind: str = "predictive"
    version: str | None = None
    alias: str | None = None
    batch_size: int = 1000
    resources: Resources = field(default_factory=Resources)
    tenant: str = "default"
    project: str = ""
    flexibility_s: float = 0.0
    schema_version: int = SCHEMA_VERSION

    # -- validation ---------------------------------------------------------------------------
    def problems(self) -> list[str]:
        out: list[str] = []
        if self.schema_version != SCHEMA_VERSION:
            out.append(f"schema_version {self.schema_version!r} is not {SCHEMA_VERSION}")
        if self.kind not in KINDS:
            out.append(f"kind must be one of {list(KINDS)}")
        if not isinstance(self.model, str) or not self.model.strip():
            out.append("model must be a non-empty string")
        has_v = self.version not in (None, "")
        has_a = self.alias not in (None, "")
        if has_v == has_a:
            out.append("exactly one of version or alias must be given")
        if has_v and not re.fullmatch(r"[0-9]+", str(self.version)):
            out.append("version must be a registry version number (digits)")
        if has_a and not isinstance(self.alias, str):
            out.append("alias must be a string")
        if not isinstance(self.idempotency_key, str) or not _KEY_RE.fullmatch(self.idempotency_key):
            out.append(
                "idempotency_key is required: 1-200 characters of letters, digits and . _ : -"
            )
        if not isinstance(self.tenant, str) or not self.tenant.strip():
            out.append("tenant must be a non-empty string")
        if not _is_int(self.batch_size) or not 1 <= self.batch_size <= MAX_BATCH_SIZE:
            out.append(f"batch_size must be an integer in 1..{MAX_BATCH_SIZE}")
        r = self.resources
        for name in ("cpus", "gpus"):
            v = getattr(r, name)
            if not _is_int(v) or v < 0:
                out.append(f"resources.{name} must be an integer >= 0")
        if not _is_num(r.memory_gb) or r.memory_gb < 0:
            out.append("resources.memory_gb must be a number >= 0")
        if not _is_num(self.flexibility_s) or self.flexibility_s < 0:
            out.append("flexibility_s must be a number >= 0")
        elif self.flexibility_s > 0:
            out.append(
                "flexibility_s > 0 asks for a carbon-aware later start; that scheduler is not "
                "built (ADR 0149 decision 2) - use 0"
            )
        out += self._input_problems() + self._output_problems()
        return out

    def _input_problems(self) -> list[str]:
        i, out = self.input, []
        if i.type not in INPUT_TYPES:
            return [f"input.type must be one of {list(INPUT_TYPES)}"]
        if i.type == "local":
            if not i.path or not isinstance(i.path, str):
                out.append("input.path is required for a local input")
            if i.source or i.table:
                out.append("input.source/table apply only to a dataplane input")
        else:
            if not i.source or not _NAME_RE.fullmatch(str(i.source)):
                out.append("input.source (a dataplane source name) is required")
            if not i.table or not _NAME_RE.fullmatch(str(i.table)):
                out.append("input.table (the snapshot table to score) is required")
            if i.revision != "latest" and not _REV_RE.fullmatch(str(i.revision)):
                out.append("input.revision must be 'latest' or a full 64-hex revision id")
            if i.path:
                out.append("input.path applies only to a local input")
        return out

    def _output_problems(self) -> list[str]:
        o, out = self.output, []
        if o.type not in OUTPUT_TYPES:
            return [f"output.type must be one of {list(OUTPUT_TYPES)}"]
        if o.type == "local":
            if not o.path or not isinstance(o.path, str):
                out.append("output.path is required for a local output")
            if o.source:
                out.append("output.source applies only to a dataplane output")
        else:
            if not o.source or not _NAME_RE.fullmatch(str(o.source)):
                out.append("output.source (the dataplane source to publish under) is required")
            if o.path:
                out.append("output.path applies only to a local output")
        return out

    def validate(self) -> OfflineJob:
        problems = self.problems()
        if problems:
            raise OfflineSpecError(problems)
        return self

    # -- serialization ------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "model": self.model,
            "version": self.version,
            "alias": self.alias,
            "input": dict(vars(self.input)),
            "output": dict(vars(self.output)),
            "batch_size": self.batch_size,
            "resources": dict(vars(self.resources)),
            "idempotency_key": self.idempotency_key,
            "tenant": self.tenant,
            "project": self.project,
            "flexibility_s": self.flexibility_s,
        }

    @classmethod
    def from_dict(cls, data: Any) -> OfflineJob:
        """Strict: unknown keys and wrong shapes are errors. Does not run :meth:`validate` (call it,
        or use :meth:`load`)."""
        if not isinstance(data, dict):
            raise OfflineSpecError(["the spec must be a JSON object"])
        problems: list[str] = []

        def _section(name: str, allowed: set[str]) -> dict[str, Any]:
            raw = data.get(name)
            if raw is None:
                return {}
            if not isinstance(raw, dict):
                problems.append(f"{name} must be an object")
                return {}
            for k in sorted(set(raw) - allowed):
                problems.append(f"unknown key {name}.{k}")
            return {k: v for k, v in raw.items() if k in allowed}

        for k in sorted(set(data) - _TOP_KEYS):
            problems.append(f"unknown key {k}")
        inp = _section("input", _INPUT_KEYS)
        outp = _section("output", _OUTPUT_KEYS)
        res = _section("resources", _RESOURCE_KEYS)
        if problems:
            raise OfflineSpecError(problems)
        top = {k: v for k, v in data.items() if k in _TOP_KEYS - {"input", "output", "resources"}}
        try:
            return cls(
                **top,
                input=InputSpec(**inp),
                output=OutputSpec(**outp),
                resources=Resources(**res),
            )
        except TypeError as exc:  # a required field (model / idempotency_key) is missing
            raise OfflineSpecError([str(exc).replace("OfflineJob.__init__() ", "")]) from exc

    @classmethod
    def load(cls, data: Any) -> OfflineJob:
        return cls.from_dict(data).validate()


def canonical_hash(doc: dict[str, Any]) -> str:
    """Stable digest of a resolved spec document."""
    blob = json.dumps(doc, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def job_id_for(tenant: str, idempotency_key: str) -> str:
    """The job's identity: a retry with the same tenant and key finds the same job."""
    digest = hashlib.sha256(f"{tenant}\0{idempotency_key}".encode()).hexdigest()
    return f"off-{digest[:20]}"
