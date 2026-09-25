"""The RAG serving endpoint (ADR 0019 decision 5): the pipeline behind an authenticated HTTP API.

Until this existed ``exa rag`` and the Skipper knowledge tool called :mod:`examlops.rag` in-process
and nothing under ``serving/`` hosted it. :func:`create_app` builds the FastAPI app that
``serving/rag_pipeline/app.py`` exposes (``uvicorn serving.rag_pipeline.app:app``; the
``rag-service`` extra). It composes platform pieces rather than re-implementing them:

* **Retrieval / generation** — :class:`examlops.rag.RagPipeline` (B5 store, B2 gateway, B1 prompt,
  C1 RETRIEVER span, ADR 0043 encoder refusal), optionally with B8 structured, citation-checked
  answers (``"structured": true``).
* **D8 guardrails on three surfaces.** The *question* is scanned with the gateway's
  :func:`examlops.gateway.default_guardrail` (``EXAMLOPS_GUARDRAIL_MODE``, monitor by default,
  enforce blocks injection → 400). *Retrieved content is untrusted* and is always passed through a
  per-tenant guardrail in ``enforce`` mode (``EXAMLOPS_RAG_CONTEXT_GUARD`` may relax it to
  ``monitor``; the built-in injection filter still neutralises it) — a poisoned document can never
  instruct the model. The *answer* is scanned on the way out (toxicity blocked → 422, PII redacted).
* **Authentication, fail closed.** Bearer tokens come from ``EXAMLOPS_RAG_TOKENS``
  (``tenant:token,…``; tenant ``*`` may act for any tenant) and/or ``EXAMLOPS_RAG_TOKEN`` (shorthand
  for ``*:<token>``). A placeholder or short (<16 chars) token is discarded at startup; with no
  usable token every API call is refused with 503 ``rag_service_unconfigured`` and ``/readyz`` is not
  ready. A token bound to one tenant cannot read or write another tenant's knowledge base (403).
* **Bounded.** Body size (``EXAMLOPS_RAG_MAX_BODY``, 8 MiB), ``k`` (1‥50), question length, docs
  per ingest (1000), concurrent requests (``EXAMLOPS_RAG_MAX_CONCURRENT``, 8 → 429 beyond) and a
  per-request deadline (``EXAMLOPS_RAG_TIMEOUT``, 30 s → 504).
* **Observable + audited.** Prometheus metrics on ``/metrics`` (own registry); ingest is audited as
  ``rag_ingest`` by the pipeline, and a refused cross-tenant call as ``rag_tenant_denied``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

_WEAK_TOKENS = frozenset(
    {"changeme", "change-me", "changeme123", "password", "secret", "admin", "token", "test"}
)
_MIN_TOKEN_LEN = 16
MAX_K = 50
MAX_DOCS = 1000
MAX_QUESTION = 4000


class _QueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    k: int = Field(default=5, ge=1, le=MAX_K)
    tenant: str | None = Field(default=None, max_length=200)
    retrieval: str = Field(default="dense", pattern="^(dense|hybrid)$")
    fusion: str = Field(default="rrf", pattern="^(rrf|convex)$")
    structured: bool = False
    prompt_label: str | None = Field(default=None, max_length=200)


class _Doc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=500)
    text: str


class _IngestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb: str = Field(min_length=1, max_length=200)
    docs: list[_Doc] = Field(min_length=1, max_length=MAX_DOCS)
    tenant: str | None = Field(default=None, max_length=200)
    source_revision: str | None = Field(default=None, max_length=200)
    framework: str | None = Field(default=None, pattern="^(native|llamaindex|auto)$")


class _Refused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class _Principal:
    tenant: str  # "*" = may act for any tenant


def parse_tokens(env: dict[str, str] | None = None) -> dict[str, str]:
    """``token -> tenant`` from the environment, weak/placeholder tokens dropped (and logged)."""
    env = dict(os.environ) if env is None else env
    pairs: list[tuple[str, str]] = []
    single = env.get("EXAMLOPS_RAG_TOKEN", "").strip()
    if single:
        pairs.append(("*", single))
    for entry in env.get("EXAMLOPS_RAG_TOKENS", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        tenant, sep, token = entry.partition(":")
        if not sep or not tenant.strip() or not token.strip():
            logger.warning("EXAMLOPS_RAG_TOKENS entry without 'tenant:token' ignored")
            continue
        pairs.append((tenant.strip(), token.strip()))
    tokens: dict[str, str] = {}
    for tenant, token in pairs:
        if token.lower() in _WEAK_TOKENS or len(token) < _MIN_TOKEN_LEN:
            # Named by tenant only — the rejected value is never logged.
            logger.warning(
                "RAG service token for tenant %r rejected: placeholder or too short", tenant
            )
            continue
        tokens[token] = tenant
    return tokens


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _context_guard(tenant: str) -> Callable[[str], tuple[bool, str]] | None:
    """Retrieved content is untrusted: scanned per tenant in enforce mode unless relaxed."""
    mode = os.getenv("EXAMLOPS_RAG_CONTEXT_GUARD", "enforce").strip().lower()
    if mode not in ("enforce", "monitor"):
        mode = "enforce"  # an unrecognised value never weakens the default
    try:
        from examlops.guardrails import DefaultGuardrail, rag_guardrail_adapter
    except Exception:  # noqa: BLE001 - the pipeline's built-in injection filter still applies
        return None
    adapter = rag_guardrail_adapter(DefaultGuardrail(mode=mode, tenant=tenant))
    if mode == "enforce":
        return adapter

    from examlops.rag import _apply_guardrail

    def _monitor(text: str) -> tuple[bool, str]:
        flagged, _ = adapter(text)  # recorded, not applied...
        f2, safe = _apply_guardrail(text)  # ...but an injection is still de-fanged
        return flagged or f2, safe

    return _monitor


def create_app(
    *,
    pipeline_factory: Callable[..., Any] | None = None,
    generate_fn: Callable[[str], Any] | None = None,
    env: dict[str, str] | None = None,
) -> FastAPI:
    """Build the RAG service. ``pipeline_factory``/``generate_fn`` are test seams."""
    from examlops.rag import RagPipeline

    tokens = parse_tokens(env)
    factory = pipeline_factory or RagPipeline
    max_body = _int_env("EXAMLOPS_RAG_MAX_BODY", 8 * 1024 * 1024)
    timeout = _float_env("EXAMLOPS_RAG_TIMEOUT", 30.0)
    slots = asyncio.Semaphore(_int_env("EXAMLOPS_RAG_MAX_CONCURRENT", 8))

    registry = CollectorRegistry()
    requests = Counter(
        "examlops_rag_requests_total",
        "RAG service requests by endpoint and outcome",
        ["endpoint", "outcome"],
        registry=registry,
    )
    latency = Histogram(
        "examlops_rag_request_seconds",
        "RAG service request latency",
        ["endpoint"],
        registry=registry,
    )
    guard_flags = Counter(
        "examlops_rag_guardrail_flags_total",
        "Guardrail findings by stage (question | context | answer)",
        ["stage"],
        registry=registry,
    )
    chunks_hist = Histogram(
        "examlops_rag_retrieved_chunks",
        "Chunks returned per query",
        buckets=(0, 1, 2, 3, 5, 8, 13, 21, 50),
        registry=registry,
    )

    app = FastAPI(title="ExaMLOps RAG service", version="1")
    app.state.tokens = tokens

    def _refuse(endpoint: str, exc: _Refused) -> JSONResponse:
        requests.labels(endpoint, exc.code).inc()
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status
        )

    def _authenticate(request: Request) -> _Principal:
        if not tokens:
            raise _Refused(
                503,
                "rag_service_unconfigured",
                "no usable token configured (EXAMLOPS_RAG_TOKEN / EXAMLOPS_RAG_TOKENS)",
            )
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            raise _Refused(401, "unauthorized", "bearer token required")
        for token, tenant in tokens.items():
            if hmac.compare_digest(token.encode(), presented.strip().encode()):
                return _Principal(tenant)
        raise _Refused(401, "unauthorized", "invalid token")

    def _tenant_for(principal: _Principal, requested: str | None, action: str, kb: str) -> str:
        if principal.tenant == "*":
            return requested or "default"
        if requested and requested != principal.tenant:
            from examlops.data.audit import audit_best_effort

            audit_best_effort(
                "rag-service",
                None,
                "rag_tenant_denied",
                f"{requested}/{kb}",
                {"action": action, "token_tenant": principal.tenant},
                tenant=principal.tenant,
            )
            raise _Refused(403, "tenant_forbidden", "token is not valid for that tenant")
        return principal.tenant

    async def _body(request: Request, model: type[BaseModel]) -> Any:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_body:
            raise _Refused(413, "body_too_large", f"request body exceeds {max_body} bytes")
        # Read the stream incrementally and stop at the cap. ``await request.body()`` buffers the
        # whole body first, so a chunked request (no Content-Length) or one that lies about its
        # length would be held in memory in full before the size check ever ran.
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > max_body:
                raise _Refused(413, "body_too_large", f"request body exceeds {max_body} bytes")
        raw = bytes(buf)
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            raise _Refused(
                400, "invalid_request", exc.errors(include_url=False)[0]["msg"]
            ) from None

    def _release(task: asyncio.Future[Any]) -> None:
        slots.release()
        if not task.cancelled():
            task.exception()  # mark retrieved: a timed-out task's error must not warn at GC

    async def _bounded(fn: Callable[[], Any]) -> Any:
        if slots.locked():
            raise _Refused(429, "overloaded", "too many concurrent RAG requests")
        await slots.acquire()
        # The slot is released when the *worker thread* finishes, not when the caller gives up: a
        # thread cannot be cancelled, so releasing on timeout would let timed-out work pile up
        # beyond the concurrency cap it exists to enforce.
        task = asyncio.ensure_future(asyncio.to_thread(fn))
        task.add_done_callback(_release)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            raise _Refused(504, "timeout", f"RAG request exceeded {timeout:g}s") from None

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        checks: dict[str, Any] = {"tokens": bool(tokens)}
        try:
            from examlops.vector_store import select_store

            select_store()
            checks["vector_store"] = True
        except Exception as exc:  # noqa: BLE001
            checks["vector_store"] = f"{type(exc).__name__}"
        ready = checks["tokens"] is True and checks["vector_store"] is True
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    @app.post("/v1/rag/query")
    async def query(request: Request) -> JSONResponse:
        started = time.perf_counter()
        try:
            principal = _authenticate(request)
            body: _QueryBody = await _body(request, _QueryBody)
            tenant = _tenant_for(principal, body.tenant, "query", body.kb)

            from examlops.gateway import default_guardrail

            qguard = default_guardrail(tenant)
            pipe = factory(retrieval=body.retrieval, fusion=body.fusion)
            guard = _context_guard(tenant)
            if guard is not None:
                pipe.guardrail = guard

            def _run() -> tuple[Any, str]:
                # Guardrail scans record to `guardrail_events` (a database write), so they run in
                # the worker thread with the query, never on the event loop, and inside the same
                # deadline and concurrency slot.
                question = body.question
                if qguard is not None:
                    res = qguard.check_input(question, {"source": "rag-service"})
                    if res.findings:
                        guard_flags.labels("question").inc()
                    if res.blocked:
                        raise _Refused(400, "guardrail_blocked", res.reason or "question blocked")
                    question = res.text
                ans = pipe.query(
                    body.kb,
                    question,
                    tenant=tenant,
                    k=body.k,
                    generate_fn=generate_fn,
                    prompt_label=body.prompt_label,
                    structured=body.structured,
                )
                answer = ans.answer
                if qguard is not None:
                    out = qguard.check_output(answer, {"source": "rag-service"})
                    if out.findings:
                        guard_flags.labels("answer").inc()
                    if out.blocked:
                        raise _Refused(422, "guardrail_blocked", out.reason or "answer blocked")
                    answer = out.text
                return ans, answer

            try:
                ans, answer = await _bounded(_run)
            except _Refused:
                raise
            except Exception as exc:  # noqa: BLE001 - mapped to a typed error below
                raise _map_error(exc) from None

            if ans.guardrail_flagged:
                guard_flags.labels("context").inc()
            chunks_hist.observe(len(ans.citations))
            payload: dict[str, Any] = {
                "kb": body.kb,
                "tenant": tenant,
                "answer": answer,
                "citations": [{"doc_id": c.doc_id, "score": c.score} for c in ans.citations],
                "guardrail_flagged": ans.guardrail_flagged,
                "retrieval_span": ans.retrieval_span_id,
            }
            if ans.structured is not None:
                payload["structured"] = {**ans.structured, "answer": answer}
                payload["grounded"] = ans.grounded
            requests.labels("query", "ok").inc()
            return JSONResponse(payload)
        except _Refused as exc:
            return _refuse("query", exc)
        finally:
            latency.labels("query").observe(time.perf_counter() - started)

    @app.post("/v1/rag/ingest")
    async def ingest(request: Request) -> JSONResponse:
        started = time.perf_counter()
        try:
            principal = _authenticate(request)
            body: _IngestBody = await _body(request, _IngestBody)
            tenant = _tenant_for(principal, body.tenant, "ingest", body.kb)
            docs = [{"id": d.id, "text": d.text} for d in body.docs]
            pipe = factory(framework=body.framework)

            def _run() -> int:
                return int(
                    pipe.ingest(body.kb, docs, tenant=tenant, source_revision=body.source_revision)
                )

            try:
                chunks = await _bounded(_run)
            except _Refused:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _map_error(exc) from None
            requests.labels("ingest", "ok").inc()
            return JSONResponse(
                {"kb": body.kb, "tenant": tenant, "docs": len(docs), "chunks": chunks}
            )
        except _Refused as exc:
            return _refuse("ingest", exc)
        finally:
            latency.labels("ingest").observe(time.perf_counter() - started)

    @app.get("/v1/rag/kbs")
    async def kbs(request: Request, tenant: str | None = None, limit: int = 100) -> JSONResponse:
        try:
            principal = _authenticate(request)
            scoped = _tenant_for(principal, tenant, "list", "*")
            limit = max(1, min(int(limit), 500))

            def _run() -> list[dict[str, Any]]:
                from examlops.data import get_db, init_db

                init_db()
                with get_db() as conn:
                    # Tenant filter in SQL, before the LIMIT — never filtered after a page is cut.
                    rows = conn.execute(
                        "SELECT kb, tenant, source_revision, encoder, chunk_count, updated_at "
                        "FROM rag_kbs WHERE tenant=? ORDER BY updated_at DESC, kb LIMIT ?",
                        (scoped, limit),
                    ).fetchall()
                return [dict(r) for r in rows]

            rows = await _bounded(_run)
            requests.labels("kbs", "ok").inc()
            return JSONResponse({"tenant": scoped, "kbs": rows})
        except _Refused as exc:
            return _refuse("kbs", exc)

    return app


def _map_error(exc: Exception) -> _Refused:
    from examlops.rag.frameworks import RagFrameworkUnavailable
    from examlops.structured import StructuredOutputError
    from examlops.vector_store import CollectionNotFound, EncoderMismatch

    if isinstance(exc, CollectionNotFound):
        return _Refused(404, "kb_not_found", str(exc))
    if isinstance(exc, EncoderMismatch):
        return _Refused(409, "encoder_mismatch", str(exc))
    if isinstance(exc, RagFrameworkUnavailable):
        return _Refused(501, "framework_unavailable", str(exc))
    if isinstance(exc, StructuredOutputError):
        return _Refused(502, "structured_output_failed", str(exc))
    logger.exception("RAG request failed")
    return _Refused(500, "internal_error", f"{type(exc).__name__}")


__all__ = ["MAX_DOCS", "MAX_K", "create_app", "parse_tokens"]
