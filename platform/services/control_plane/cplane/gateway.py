"""The Prefect client: pooled httpx, one deadline per dispatch, and a circuit breaker.

Plan P1.5. Configuration is read from the environment at import, like the rest of the service."""

from __future__ import annotations

import contextvars
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, TypeVar

import httpx
import metrics as _metrics
from fastapi import HTTPException, status

T = TypeVar("T")
logger = logging.getLogger("control_plane")

PREFECT_API_URL = os.getenv("PREFECT_API_URL", "http://localhost:14200/api").rstrip("/")
PREFECT_CB_FAIL_MAX = int(os.getenv("PREFECT_CB_FAIL_MAX", "5"))
PREFECT_CB_RESET_TIMEOUT = float(os.getenv("PREFECT_CB_RESET_TIMEOUT", "30.0"))


# ─── Improvement 12: Prefect circuit breaker ─────────────────────────────────


class _CircuitBreaker:
    """Three-state circuit breaker: CLOSED → OPEN → HALF-OPEN → CLOSED.

    Opens after PREFECT_CB_FAIL_MAX consecutive upstream failures.
    After PREFECT_CB_RESET_TIMEOUT seconds in OPEN state, allows one trial (HALF-OPEN).
    A successful trial closes the breaker; failure re-opens it immediately.
    """

    _CLOSED = "closed"
    _OPEN = "open"
    _HALF_OPEN = "half-open"

    def __init__(self, fail_max: int = 5, reset_timeout: float = 30.0) -> None:
        self._fail_max = fail_max
        self._reset_timeout = reset_timeout
        self._failures = 0
        self._state = self._CLOSED
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def call(self, fn: Callable[[], T]) -> T:
        """Execute fn, tracking failures. Raises 503 when circuit is open."""
        with self._lock:
            if self._state == self._OPEN:
                if time.monotonic() - self._opened_at >= self._reset_timeout:
                    self._state = self._HALF_OPEN
                    logger.info("Prefect circuit breaker HALF-OPEN — probing")
                else:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Prefect circuit breaker open — upstream unavailable, retry later",
                    )

        try:
            result = fn()
            with self._lock:
                if self._state == self._HALF_OPEN:
                    logger.info("Prefect circuit breaker CLOSED — upstream recovered")
                self._state = self._CLOSED
                self._failures = 0
            return result
        except HTTPException as exc:
            if exc.status_code >= 500:
                self._on_failure()
            raise
        except Exception:
            self._on_failure()
            raise

    def _on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._fail_max or self._state == self._HALF_OPEN:
                self._state = self._OPEN
                self._opened_at = time.monotonic()
                logger.error(
                    "Prefect circuit breaker OPENED after %d consecutive failures", self._failures
                )
                _metrics.record_circuit_breaker_open()


_prefect_breaker = _CircuitBreaker(
    fail_max=PREFECT_CB_FAIL_MAX,
    reset_timeout=PREFECT_CB_RESET_TIMEOUT,
)


# ─── Prefect client (improvement 3 retry + improvement 12 circuit breaker) ───

PREFECT_CONNECT_TIMEOUT = float(os.getenv("CONTROL_PLANE_PREFECT_CONNECT_TIMEOUT", "3"))
PREFECT_READ_TIMEOUT = float(os.getenv("CONTROL_PLANE_PREFECT_READ_TIMEOUT", "5"))
PREFECT_MAX_ATTEMPTS = max(1, int(os.getenv("CONTROL_PLANE_PREFECT_ATTEMPTS", "3")))
PREFECT_BACKOFF_BASE = 0.25
# Total budget for one dispatch (deployment lookup + flow-run creation). Below the 10 s client
# timeout every caller uses, so the server answers before the client gives up on it.
PREFECT_CALL_BUDGET = float(os.getenv("CONTROL_PLANE_DISPATCH_BUDGET_SECONDS", "8"))
_dispatch_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "dispatch_deadline", default=None
)


@contextmanager
def _dispatch_budget() -> Any:
    """Share one deadline across every Prefect call of a single dispatch."""
    token = _dispatch_deadline.set(time.monotonic() + PREFECT_CALL_BUDGET)
    try:
        yield
    finally:
        _dispatch_deadline.reset(token)


class PrefectGateway:
    """Pooled httpx Prefect REST client: deadline-bounded jittered retry + circuit breaker."""

    def __init__(self, api_url: str = PREFECT_API_URL) -> None:
        self.api_url = api_url.rstrip("/")
        self._http: httpx.Client | None = None
        self._http_lock = threading.Lock()

    def get_deployment(self, deployment_name: str) -> dict[str, Any]:
        """The Prefect deployment document, or a 503 that says how to create it.

        A missing dispatch target is a platform misconfiguration, not a missing *API route*: it
        used to surface as Prefect's bare 404, which every caller read as "this endpoint does not
        exist" (finding B2).
        """
        import urllib.parse  # noqa: PLC0415

        if "/" not in deployment_name:
            raise HTTPException(400, f"deployment must be 'flow/name', got {deployment_name!r}")
        flow_name, dep_name = deployment_name.split("/", 1)
        url = (
            f"{self.api_url}/deployments/name/"
            f"{urllib.parse.quote(flow_name)}/{urllib.parse.quote(dep_name)}"
        )
        try:
            payload = _prefect_breaker.call(lambda: self._get(url))
        except HTTPException as exc:
            if exc.status_code == 404:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    f"Prefect deployment {deployment_name!r} does not exist. Register and serve "
                    "it with `exa pipeline deploy` (or set PREFECT_DEPLOYMENT_NAME).",
                ) from exc
            raise
        if not payload.get("id"):
            raise HTTPException(502, f"Prefect returned no id for {deployment_name!r}")
        return payload

    def find_deployment_id(self, deployment_name: str) -> str:
        return str(self.get_deployment(deployment_name)["id"])

    def create_flow_run(
        self,
        deployment_id: str,
        parameters: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        url = f"{self.api_url}/deployments/{deployment_id}/create_flow_run"
        body: dict[str, Any] = {"parameters": parameters}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        payload = _prefect_breaker.call(lambda: self._post(url, body))
        run_id = payload.get("id")
        if not run_id:
            raise HTTPException(502, "Prefect create_flow_run returned no id")
        return run_id

    def find_flow_run_by_key(self, idempotency_key: str) -> str | None:
        """The id of the flow run Prefect holds for this idempotency key, or ``None``.

        Read-only on purpose. The obvious way to ask "does a run exist for this key?" is to call
        `create_flow_run` again and see whether Prefect returns the existing one — which creates it
        when it does not, resurrecting exactly the work the caller is about to bury.

        Used when a command is given up on: a dispatch that was merely slow can land after the
        platform has stopped waiting, so a dead command may still have a training run behind it
        (measured in `tests/integration/test_control_plane_partition_kind_live.py`). Best effort —
        the caller treats every failure as "unknown", because this must never stop a burial.
        """
        body = {"flow_runs": {"idempotency_key": {"any_": [idempotency_key]}}, "limit": 1}
        found: Any = _prefect_breaker.call(
            lambda: self._request("POST", f"{self.api_url}/flow_runs/filter", body, retryable=True)
        )
        runs = found if isinstance(found, list) else (found or {}).get("items") or []
        return str(runs[0]["id"]) if runs else None

    def get_flow_run(self, flow_run_id: str) -> dict[str, Any]:
        return _prefect_breaker.call(lambda: self._get(f"{self.api_url}/flow_runs/{flow_run_id}"))

    def _get(self, url: str) -> dict[str, Any]:
        return self._request("GET", url, None, retryable=True)

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        # A POST is retried only when Prefect can deduplicate it: every control-plane dispatch
        # carries an idempotency key, so a retry after a lost response cannot create a second run.
        return self._request("POST", url, body, retryable="idempotency_key" in body)

    def _client(self) -> httpx.Client:
        if self._http is None:
            with self._http_lock:
                if self._http is None:
                    # One pooled client per gateway: urllib opened a new TCP connection per call
                    # and is invisible to the httpx OpenTelemetry instrumentation.
                    from examlops.service_auth import prefect_headers

                    # Prefect's API auth string / key when the server requires one (plan P3.6).
                    self._http = httpx.Client(
                        headers={"Accept": "application/json", **prefect_headers()},
                        timeout=httpx.Timeout(
                            PREFECT_READ_TIMEOUT, connect=PREFECT_CONNECT_TIMEOUT
                        ),
                    )
        return self._http

    def _request(
        self, method: str, url: str, body: dict[str, Any] | None, *, retryable: bool
    ) -> dict[str, Any]:
        """One Prefect call inside the dispatch deadline (plan P1.5 / finding P2).

        Retries with full-jitter backoff, but never past the deadline the dispatch started with:
        the old loop slept 0.5 + 1 + 2 s between three 10 s attempts, per call, twice per retrain —
        up to ~67 s holding a request thread while every client had given up after 10 s.
        """
        deadline = _dispatch_deadline.get() or (time.monotonic() + PREFECT_CALL_BUDGET)
        attempts = PREFECT_MAX_ATTEMPTS if retryable else 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                break
            try:
                timeout = httpx.Timeout(
                    min(PREFECT_READ_TIMEOUT, remaining),
                    connect=min(PREFECT_CONNECT_TIMEOUT, remaining),
                )
                resp = self._client().request(method, url, json=body, timeout=timeout)
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code < 500:
                    raise HTTPException(
                        resp.status_code, f"Prefect {method} {url} -> HTTP {resp.status_code}"
                    )
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
            if attempt == attempts:
                break
            _metrics.record_prefect_retry(method)
            pause = min(
                random.uniform(0, PREFECT_BACKOFF_BASE * 2 ** (attempt - 1)),
                max(0.0, deadline - time.monotonic() - 0.05),
            )
            logger.warning(
                "Prefect %s %s failed (attempt %d/%d): %s; retrying in %.2fs",
                method,
                url,
                attempt,
                attempts,
                last_exc,
                pause,
            )
            time.sleep(pause)
        raise HTTPException(502, f"Prefect unreachable within the dispatch deadline: {last_exc}")
