"""OTLP/HTTP (protobuf) span exporter and agent-observability consumers (ADR 0021 decision 2).

The platform's in-process exporter speaks OTLP over **gRPC** — which Tempo and Arize Phoenix
accept, and which Langfuse does not: Langfuse ingests OTLP over **HTTP** only, at
``<host>/api/public/otel/v1/traces``. This module closes that hop without a collector.

* :func:`build_http_exporter` returns the upstream ``opentelemetry-exporter-otlp-proto-http``
  exporter when that optional extra (``examlops[agentops]``) is installed, and otherwise
  :class:`OTLPHttpSpanExporter` — a small stdlib exporter that encodes with the same
  ``opentelemetry-exporter-otlp-proto-common`` encoder the gRPC exporter already pulls in, so
  the fallback adds no dependency.
* :func:`consumer_exporters` builds one exporter per agent-observability consumer listed in
  ``EXAMLOPS_OTEL_CONSUMERS`` (``langfuse``, ``phoenix``). OpenTelemetry stays the source of
  truth: a consumer is an *additional* span processor next to the primary OTLP exporter, never a
  replacement, so switching one off loses nothing in Tempo.

Security defaults (fail closed):

* A consumer is exported to **only** when it is listed — credentials in the environment for
  some other purpose never start a data flow on their own.
* A consumer whose endpoint is not ``https`` is refused unless its host is loopback, because the
  Langfuse credential is an HTTP Basic header and a Phoenix key a bearer header. Plain HTTP to a
  remote collector would publish them. ``EXAMLOPS_OTEL_ALLOW_INSECURE=1`` is the explicit dev
  opt-out.
* Redirects are never followed: a redirect would replay the credential header to another host.
* Every request is bounded by a timeout (``OTEL_EXPORTER_OTLP_TIMEOUT``, milliseconds per the
  OTel spec, default 10 000) and a request body the encoder produced — nothing unbounded is read
  back from the collector.
"""

from __future__ import annotations

import base64
import gzip
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

log = logging.getLogger("examlops.telemetry.otlp_http")

_TRUTHY = {"1", "true", "yes", "on"}
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
#: Status codes the OTLP/HTTP spec calls retryable.
_RETRYABLE = {429, 502, 503, 504}
#: Upper bound on attempts per export batch (1 try + 2 retries), so a dead collector costs a
#: bounded amount of the batch processor's time rather than stalling its queue.
MAX_ATTEMPTS = 3
#: What is read back from a collector response at most. OTLP success bodies are tiny; an error
#: body is only logged, so it is never worth buffering more than this.
_MAX_RESPONSE_BYTES = 64 * 1024

CONSUMERS = ("langfuse", "phoenix")


class ConsumerConfigError(ValueError):
    """A listed consumer cannot be exported to safely (missing config or insecure endpoint)."""


def _timeout_seconds() -> float:
    raw = os.getenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT") or os.getenv("OTEL_EXPORTER_OTLP_TIMEOUT")
    try:
        ms = float(raw) if raw else 10_000.0
    except ValueError:
        ms = 10_000.0
    return min(max(ms / 1000.0, 0.5), 60.0)


def parse_headers(raw: str | None) -> dict[str, str]:
    """``key=value,key2=value2`` (the ``OTEL_EXPORTER_OTLP_HEADERS`` format) → dict."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            out[unquote(key.strip()).lower()] = unquote(value.strip())
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a: Any, **_k: Any) -> None:  # type: ignore[override]
        return None


class OTLPHttpSpanExporter:
    """Minimal OTLP/HTTP protobuf ``SpanExporter`` (stdlib transport, upstream encoder).

    Implements the SDK's exporter protocol — ``export`` / ``shutdown`` / ``force_flush`` — so it
    plugs into a ``BatchSpanProcessor`` exactly like the upstream class.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        compression: bool = True,
        name: str = "otlp-http",
    ) -> None:
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.timeout = timeout if timeout is not None else _timeout_seconds()
        self.compression = compression
        self.name = name
        self._shutdown = False
        self._opener = urllib.request.build_opener(_NoRedirect)
        self.exported = 0  # spans accepted by the collector, this process
        self.failed = 0  # spans dropped after the last attempt, this process

    def export(self, spans: Sequence[Any]) -> Any:
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from opentelemetry.sdk.trace.export import SpanExportResult

        if self._shutdown:
            return SpanExportResult.FAILURE
        if not spans:
            return SpanExportResult.SUCCESS
        body = encode_spans(spans).SerializePartialToString()
        headers = {"Content-Type": "application/x-protobuf", **self.headers}
        if self.compression:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        for attempt in range(MAX_ATTEMPTS):
            request = urllib.request.Request(self.endpoint, data=body, headers=headers)
            try:
                with self._opener.open(request, timeout=self.timeout) as resp:
                    resp.read(_MAX_RESPONSE_BYTES)
                    if 200 <= resp.status < 300:
                        self.exported += len(spans)
                        return SpanExportResult.SUCCESS
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except Exception as exc:  # noqa: BLE001 - connection refused, timeout, DNS
                log.warning("otlp-http export to %s failed: %s", self.name, type(exc).__name__)
                status = None
            if status is not None and status not in _RETRYABLE:
                log.warning("otlp-http export to %s rejected with HTTP %s", self.name, status)
                break
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(min(0.25 * (2**attempt), 2.0))
        self.failed += len(spans)
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._shutdown = True

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def build_http_exporter(
    endpoint: str, *, headers: dict[str, str] | None = None, name: str = "otlp-http"
) -> Any:
    """The upstream OTLP/HTTP exporter when the ``agentops`` extra is installed, else ours."""
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as UpstreamHttpExporter,
        )

        return UpstreamHttpExporter(
            endpoint=endpoint, headers=dict(headers or {}), timeout=_timeout_seconds()
        )
    except ImportError:
        return OTLPHttpSpanExporter(endpoint, headers=headers, name=name)


def traces_endpoint(base: str) -> str:
    """Per the OTel spec, a signal-agnostic base endpoint gets ``/v1/traces`` appended."""
    return base.rstrip("/") + "/v1/traces"


def _require_secure(name: str, url: str, *, carries_credential: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConsumerConfigError(f"{name}: endpoint {url!r} is not an http(s) URL")
    if parts.scheme == "https" or not carries_credential:
        return
    if parts.hostname in _LOOPBACK:
        return
    if os.getenv("EXAMLOPS_OTEL_ALLOW_INSECURE", "").strip().lower() in _TRUTHY:
        log.warning("%s: sending a credential over plain HTTP to %s (insecure opt-in)", name, url)
        return
    raise ConsumerConfigError(
        f"{name}: refusing to send a credential over plain HTTP to {parts.hostname} — use https, "
        "a loopback collector, or set EXAMLOPS_OTEL_ALLOW_INSECURE=1 for a dev setup"
    )


@dataclass(frozen=True)
class ConsumerTarget:
    """Where one consumer's spans go (no credential values in ``repr``)."""

    name: str
    endpoint: str
    headers: dict[str, str]

    def __repr__(self) -> str:  # never print the auth header
        return f"ConsumerTarget(name={self.name!r}, endpoint={self.endpoint!r})"


def langfuse_target() -> ConsumerTarget:
    """Langfuse's OTLP/HTTP endpoint + Basic auth, from Langfuse's own environment variables.

    There is deliberately **no default host**: exporting to Langfuse Cloud would ship traces to a
    SaaS the operator never named, which the self-hosted posture (ADR 0021 alternatives) rules out.
    """
    host = (os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL") or "").strip()
    public = (os.getenv("LANGFUSE_PUBLIC_KEY") or "").strip()
    secret = (os.getenv("LANGFUSE_SECRET_KEY") or "").strip()
    missing = [
        n
        for n, v in (
            ("LANGFUSE_HOST", host),
            ("LANGFUSE_PUBLIC_KEY", public),
            ("LANGFUSE_SECRET_KEY", secret),
        )
        if not v
    ]
    if missing:
        raise ConsumerConfigError(f"langfuse: {', '.join(missing)} not set")
    endpoint = host.rstrip("/") + "/api/public/otel/v1/traces"
    _require_secure("langfuse", endpoint, carries_credential=True)
    token = base64.b64encode(f"{public}:{secret}".encode()).decode("ascii")
    return ConsumerTarget("langfuse", endpoint, {"authorization": f"Basic {token}"})


def phoenix_target() -> ConsumerTarget:
    """Arize Phoenix's OTLP/HTTP endpoint (``PHOENIX_COLLECTOR_ENDPOINT``), optional API key."""
    base = (os.getenv("PHOENIX_COLLECTOR_ENDPOINT") or "").strip()
    if not base:
        raise ConsumerConfigError("phoenix: PHOENIX_COLLECTOR_ENDPOINT not set")
    endpoint = base if base.rstrip("/").endswith("/v1/traces") else traces_endpoint(base)
    key = (os.getenv("PHOENIX_API_KEY") or "").strip()
    _require_secure("phoenix", endpoint, carries_credential=bool(key))
    headers = {"authorization": f"Bearer {key}"} if key else {}
    return ConsumerTarget("phoenix", endpoint, headers)


_TARGETS = {"langfuse": langfuse_target, "phoenix": phoenix_target}


def requested_consumers() -> list[str]:
    raw = os.getenv("EXAMLOPS_OTEL_CONSUMERS", "")
    return [c.strip().lower() for c in raw.split(",") if c.strip()]


def consumer_targets() -> tuple[list[ConsumerTarget], list[str]]:
    """``(targets, problems)`` for every consumer listed in ``EXAMLOPS_OTEL_CONSUMERS``.

    A consumer that cannot be configured safely is reported in ``problems`` and skipped — never
    exported to with a partial or insecure configuration, and never allowed to stop the service
    from starting (the primary trace pipeline is unaffected by a consumer's misconfiguration).
    """
    targets: list[ConsumerTarget] = []
    problems: list[str] = []
    for name in dict.fromkeys(requested_consumers()):
        build = _TARGETS.get(name)
        if build is None:
            problems.append(f"{name}: unknown consumer (expected one of {', '.join(CONSUMERS)})")
            continue
        try:
            targets.append(build())
        except ConsumerConfigError as exc:
            problems.append(str(exc))
    return targets, problems


def consumer_exporters() -> list[tuple[str, Any]]:
    """One OTLP/HTTP exporter per safely-configured consumer; problems are logged."""
    targets, problems = consumer_targets()
    for problem in problems:
        log.warning("agent-observability consumer skipped — %s", problem)
    return [
        (t.name, build_http_exporter(t.endpoint, headers=t.headers, name=t.name)) for t in targets
    ]


__all__ = [
    "CONSUMERS",
    "ConsumerConfigError",
    "ConsumerTarget",
    "OTLPHttpSpanExporter",
    "build_http_exporter",
    "consumer_exporters",
    "consumer_targets",
    "langfuse_target",
    "parse_headers",
    "phoenix_target",
    "traces_endpoint",
]
