"""RAG serving endpoint (ADR 0019 decision 5) — the ASGI entrypoint.

    uvicorn serving.rag_pipeline.app:app --host 127.0.0.1 --port 18011

(``pip install 'examlops[rag-service]'``). The service itself lives in :mod:`examlops.rag.service`
so this entrypoint and the tests build the one app; this module only instantiates it. Configuration is
environment-only (``EXAMLOPS_RAG_TOKEN``/``EXAMLOPS_RAG_TOKENS``, ``EXAMLOPS_GUARDRAIL_MODE``,
``EXAMLOPS_RAG_CONTEXT_GUARD``, ``EXAMLOPS_RAG_MAX_BODY``/``_TIMEOUT``/``_MAX_CONCURRENT``, plus the
vector-store and gateway settings the pipeline already reads).
"""

from examlops.rag.service import create_app

app = create_app()
