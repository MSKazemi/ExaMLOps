"""Live-traffic sampler for online evaluation (ADR 0007 decision 2).

The decision: "a traffic sampler pulls recent inferences by ``request_hash`` (from C1 spans /
``platform_db``) for online eval". Until now :func:`~examlops.evaluation.sample_by_request_hash`
only *ordered* items a caller supplied; nothing pulled traffic. Two sources do now, one per place
an inference is recorded:

* :class:`PredictionsSource` — ``platform_db.predictions`` joined to the newest
  ``ground_truth`` label for the same ``request_hash`` (the #9 feedback loop). This is the
  predictive path: output = the prediction, reference = the label. Only **labelled** predictions
  are returned — an unlabelled numeric prediction cannot be scored, and label-free estimation is
  the C5 detectors' job (``exa drift run-advanced``). ``predictions`` carries no tenant column, so
  this source refuses any tenant but ``default`` rather than return another tenant's traffic.
* :class:`TempoSource` — GenAI spans in Grafana Tempo (the C1 OTLP→Tempo pipeline), searched with
  TraceQL for ``gen_ai.request.model`` + ``examlops.tenant`` + a present ``examlops.request_hash``.
  The model and tenant filters are **in the query**, so the server applies them before its own
  ``limit``. Prompt/completion text exists on a span only when ``EXAMLOPS_GENAI_CAPTURE_CONTENT``
  was on (and it is always redacted by D8 first); a span without it is counted as
  ``no_content`` and skipped, and the report says so, because "no items" and "capture is off"
  must not look the same.

Both are bounded (``limit`` ≤ :data:`MAX_LIMIT`, an HTTP timeout on Tempo), both deduplicate by
``request_hash`` keeping the newest, and both return :class:`~examlops.evaluation.EvalItem`
carrying the **recorded** hash so the online sample is the deterministic
:func:`~examlops.evaluation.sample_by_request_hash` of real requests.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from examlops.evaluation import EvalItem

#: Hard cap on rows/spans one pull may return, whatever the caller asks for.
MAX_LIMIT = 5000
DEFAULT_LIMIT = 1000
TEMPO_URL_ENV = "EXAMLOPS_EVAL_TEMPO_URL"
TEMPO_TIMEOUT_ENV = "EXAMLOPS_EVAL_TEMPO_TIMEOUT"
TEMPO_TOKEN_ENV = "EXAMLOPS_EVAL_TEMPO_TOKEN"
TEMPO_ORG_ENV = "EXAMLOPS_EVAL_TEMPO_ORG"
DEFAULT_TEMPO_TIMEOUT_S = 10.0

SOURCES = ("predictions", "tempo")

#: What a model / tenant / alias name may contain before it is spliced into TraceQL or SQL.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


class TrafficSourceError(RuntimeError):
    """The traffic source could not be read (unreachable, misconfigured, or refused)."""


def safe_name(value: str, what: str) -> str:
    """Refuse a name that could break out of a TraceQL string literal (fail closed)."""
    if not _SAFE_NAME.match(value or ""):
        raise TrafficSourceError(f"{what} {value!r} is not a valid name")
    return value


@dataclass
class TrafficPull:
    """What one pull returned, and why anything that was seen is not in ``items``."""

    source: str
    items: list[EvalItem] = field(default_factory=list)
    seen: int = 0
    no_content: int = 0
    duplicates: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "items": len(self.items),
            "seen": self.seen,
            "no_content": self.no_content,
            "duplicates": self.duplicates,
            "note": self.note,
        }


class TrafficSource(Protocol):
    name: str

    def pull(
        self, model: str, *, since_s: int, limit: int, tenant: str, alias: str | None
    ) -> TrafficPull: ...


def _bounded(limit: int) -> int:
    return max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))


def _sql_ts(epoch: float) -> str:
    """``CURRENT_TIMESTAMP``'s own format (UTC, second resolution)."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class PredictionsSource:
    """Labelled predictions from ``platform_db`` (the predictive path)."""

    name: str = "predictions"
    clock: Callable[[], float] = time.time

    def pull(
        self,
        model: str,
        *,
        since_s: int,
        limit: int = DEFAULT_LIMIT,
        tenant: str = "default",
        alias: str | None = None,
    ) -> TrafficPull:
        if tenant != "default":
            raise TrafficSourceError(
                "the predictions table carries no tenant, so it cannot be scoped to "
                f"tenant {tenant!r}; use the tempo source for tenant-scoped traffic"
            )
        from examlops.data.evaluation import recent_labelled_predictions

        rows = recent_labelled_predictions(
            model,
            since=_sql_ts(self.clock() - max(1, int(since_s))),
            alias=alias,
            limit=_bounded(limit),
        )
        pull = TrafficPull(self.name, seen=len(rows))
        for r in rows:
            pull.items.append(
                EvalItem(
                    output=repr(float(r["prediction"])),
                    reference=repr(float(r["label"])),
                    metadata={"alias": r["alias"], "ts": str(r["ts"]), "source": self.name},
                    recorded_hash=str(r["request_hash"]),
                )
            )
        if not rows:
            pull.note = "no labelled predictions in the window"
        return pull


def _attr_value(value: dict[str, Any]) -> Any:
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return None


def _messages_text(raw: Any) -> str | None:
    """Text of a ``gen_ai.*.messages`` JSON attribute (the latest-experimental shape)."""
    if not isinstance(raw, str):
        return None
    try:
        messages = json.loads(raw)
    except ValueError:
        return raw
    parts: list[str] = []
    for message in messages if isinstance(messages, list) else []:
        for part in message.get("parts", []) if isinstance(message, dict) else []:
            if isinstance(part, dict) and part.get("type", "text") == "text":
                parts.append(str(part.get("content", "")))
    return "\n".join(parts) if parts else None


_SELECT = (
    "span.examlops.request_hash, span.gen_ai.prompt, span.gen_ai.completion, "
    "span.gen_ai.input.messages, span.gen_ai.output.messages, span.examlops.model.alias"
)


def traceql(model: str, tenant: str) -> str:
    """The TraceQL search for one model's GenAI spans in one tenant. Names are pre-validated."""
    safe_name(model, "model")
    safe_name(tenant, "tenant")
    return (
        f'{{ span.gen_ai.request.model = "{model}" && span.examlops.tenant = "{tenant}" '
        f"&& span.examlops.request_hash != nil }} | select({_SELECT})"
    )


HttpGet = Callable[..., Any]


@dataclass
class TempoSource:
    """GenAI spans from Grafana Tempo's search API (the C1 span store)."""

    base_url: str | None = None
    http_get: HttpGet | None = None
    timeout_s: float | None = None
    name: str = "tempo"
    clock: Callable[[], float] = time.time

    def _url(self) -> str:
        url = (self.base_url or os.getenv(TEMPO_URL_ENV) or "").strip().rstrip("/")
        if not url:
            raise TrafficSourceError(
                f"no Tempo URL — set {TEMPO_URL_ENV} (e.g. http://tempo:3200) to sample spans"
            )
        if not url.startswith(("http://", "https://")):
            raise TrafficSourceError(f"{TEMPO_URL_ENV} must be an http(s) URL, got {url!r}")
        return url

    def _timeout(self) -> float:
        if self.timeout_s is not None:
            return float(self.timeout_s)
        try:
            return float(os.getenv(TEMPO_TIMEOUT_ENV, "") or DEFAULT_TEMPO_TIMEOUT_S)
        except ValueError:
            return DEFAULT_TEMPO_TIMEOUT_S

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        token = os.getenv(TEMPO_TOKEN_ENV, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        org = os.getenv(TEMPO_ORG_ENV, "").strip()
        if org:
            headers["X-Scope-OrgID"] = org
        return headers

    def pull(
        self,
        model: str,
        *,
        since_s: int,
        limit: int = DEFAULT_LIMIT,
        tenant: str = "default",
        alias: str | None = None,
    ) -> TrafficPull:
        url = self._url()
        end = int(self.clock())
        params = {
            "q": traceql(model, tenant),
            "start": str(end - max(1, int(since_s))),
            "end": str(end),
            "limit": str(_bounded(limit)),
            "spss": "20",
        }
        get = self.http_get
        if get is None:
            import httpx

            get = httpx.get
        try:
            resp = get(
                f"{url}/api/search", params=params, headers=self._headers(), timeout=self._timeout()
            )
            status = int(getattr(resp, "status_code", 200))
            if status >= 400:
                raise TrafficSourceError(f"Tempo search answered HTTP {status}")
            body = resp.json()
        except TrafficSourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - network/JSON failure is a source error
            raise TrafficSourceError(f"Tempo search failed: {type(exc).__name__}: {exc}") from exc
        return self._parse(body, alias=alias, limit=_bounded(limit))

    def _parse(self, body: Any, *, alias: str | None, limit: int) -> TrafficPull:
        pull = TrafficPull(self.name)
        rows: list[tuple[int, dict[str, Any]]] = []
        for trace in (body or {}).get("traces", []) if isinstance(body, dict) else []:
            span_sets = trace.get("spanSets") or ([trace["spanSet"]] if "spanSet" in trace else [])
            for span_set in span_sets:
                for span in (span_set or {}).get("spans", []):
                    attrs = {
                        a.get("key"): _attr_value(a.get("value") or {})
                        for a in span.get("attributes", [])
                    }
                    started = int(
                        span.get("startTimeUnixNano") or trace.get("startTimeUnixNano") or 0
                    )
                    rows.append((started, attrs))
        rows.sort(key=lambda r: r[0], reverse=True)  # newest first, then dedupe keeps newest
        seen_hashes: set[str] = set()
        for _started, attrs in rows:
            pull.seen += 1
            request_hash = attrs.get("examlops.request_hash")
            if not request_hash:
                continue
            span_alias = attrs.get("examlops.model.alias")
            if alias and span_alias and span_alias != alias:
                continue
            if request_hash in seen_hashes:
                pull.duplicates += 1
                continue
            completion = attrs.get("gen_ai.completion") or _messages_text(
                attrs.get("gen_ai.output.messages")
            )
            if not completion:
                pull.no_content += 1
                continue
            prompt = attrs.get("gen_ai.prompt") or _messages_text(
                attrs.get("gen_ai.input.messages")
            )
            seen_hashes.add(request_hash)
            pull.items.append(
                EvalItem(
                    output=str(completion),
                    prompt=str(prompt) if prompt else None,
                    metadata={"alias": span_alias, "source": self.name},
                    recorded_hash=str(request_hash),
                )
            )
            if len(pull.items) >= limit:
                break
        if pull.no_content and not pull.items:
            pull.note = (
                "spans carry no prompt/completion content — enable "
                "EXAMLOPS_GENAI_CAPTURE_CONTENT on the serving path to evaluate live traffic"
            )
        elif not pull.seen:
            pull.note = "no matching spans in the window"
        return pull


def get_source(name: str, **kwargs: Any) -> TrafficSource:
    """The traffic source a config row names."""
    if name == "predictions":
        return PredictionsSource(**kwargs)
    if name == "tempo":
        return TempoSource(**kwargs)
    raise TrafficSourceError(f"unknown traffic source {name!r}; known: {', '.join(SOURCES)}")


__all__ = [
    "MAX_LIMIT",
    "SOURCES",
    "PredictionsSource",
    "TempoSource",
    "TrafficPull",
    "TrafficSource",
    "TrafficSourceError",
    "get_source",
    "safe_name",
    "traceql",
]
