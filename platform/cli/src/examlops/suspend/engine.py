"""``vllm-sleep`` - engine-delegated suspend of a vLLM serving replica (ADR 0109 decision 5).

vLLM's *sleep mode* releases a running engine's accelerator memory without stopping its
processes: at level 1 the weights are offloaded to host RAM and the KV cache is discarded;
``wake_up`` moves the weights back. The server exposes it over HTTP when started with
``--enable-sleep-mode`` and ``VLLM_SERVER_DEV_MODE=1``:

* ``POST /sleep?level=1``  - release GPU memory
* ``POST /wake_up``        - re-acquire it
* ``GET  /is_sleeping``    - ``{"is_sleeping": bool}``

This backend drives exactly those three endpoints and nothing else, and its ``capability()`` is
deliberately narrow:

* **state is in host RAM of the same node** (``local_memory``): it does not survive losing the
  node, so :func:`~examlops.suspend.cost.preemption_promise` correctly declines checkpoint-
  preserving preemption on it. It serves the scale-to-zero and grid-damping drivers, not
  preemption.
* ``gpu_state`` is ``False``: the KV cache is discarded, so a caller that asks for GPU state is
  refused rather than handed a partial one.
* ``communicator_rebuild_applicable`` is ``False``: the engine's worker processes and their
  process groups stay alive across sleep, so nothing is rebuilt.
* Level 2 (weights discarded, wake needs a weight reload) is refused: restoring from it is a
  second protocol this backend does not drive.

Every call is bounded by a timeout (``EXAMLOPS_SUSPEND_VLLM_TIMEOUT``, default 60 s); the address
is operator-supplied (``options['base_url']`` or ``EXAMLOPS_SUSPEND_VLLM_URL``) and must be
``http``/``https``. A bearer key (``EXAMLOPS_VLLM_API_KEY``) is **origin-bound**: it is sent only
to the scheme+host+port of the operator-configured ``EXAMLOPS_SUSPEND_VLLM_URL``, never to an
address supplied per call (``--base-url``) or read back from a stored snapshot pointer that names a
different origin - otherwise anyone able to pass a URL could have the platform hand the key to a
host of their choosing.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx

from .backends import _BackendBase
from .types import (
    STATE_SERVING_REPLICA,
    Capability,
    RestoreReport,
    SnapshotHandle,
    SuspendError,
    SuspendUnsupported,
)

URL_ENV = "EXAMLOPS_SUSPEND_VLLM_URL"
TIMEOUT_ENV = "EXAMLOPS_SUSPEND_VLLM_TIMEOUT"
_DEFAULT_TIMEOUT = 60.0
SUPPORTED_LEVEL = 1


class VLLMUnreachable(SuspendError):
    """The engine could not be asked (transport error, timeout, 5xx). Says nothing about its state.

    ``restore`` raises this instead of reporting ``restored=False``: a failed report marks the
    snapshot ``failed`` for good, which would strand a still-sleeping engine with no seam path to
    wake it. Raised, the record stays ``suspended`` and the resume can be retried.
    """


def _timeout() -> float:
    raw = os.getenv(TIMEOUT_ENV, "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_TIMEOUT
    except ValueError:
        value = _DEFAULT_TIMEOUT
    return min(max(value, 1.0), 600.0)


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urlparse(url)
    port = p.port or {"http": 80, "https": 443}.get(p.scheme)
    return (p.scheme.lower(), (p.hostname or "").lower(), port)


def _key_for(base_url: str) -> str:
    """The API key, only when ``base_url`` is the configured server's origin (origin binding)."""
    key = os.getenv("EXAMLOPS_VLLM_API_KEY", "")
    configured = os.getenv(URL_ENV, "").strip()
    if not key or not configured:
        return ""
    try:
        return key if _origin(configured) == _origin(base_url) else ""
    except ValueError:  # an unparsable port is not the configured origin
        return ""


def _base_url(options: dict[str, Any] | None) -> str:
    raw = str((options or {}).get("base_url") or os.getenv(URL_ENV, "")).strip().rstrip("/")
    if not raw:
        raise SuspendError(f"no vLLM server address: pass base_url or set {URL_ENV}")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SuspendError(f"vLLM base_url must be an http(s) URL, got {raw!r}")
    return raw


class VLLMSleepBackend(_BackendBase):
    """Suspend a vLLM replica by putting its engine to sleep; resume by waking it."""

    name = "vllm-sleep"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def capability(self) -> Capability:
        return Capability(
            backend=self.name,
            granularity="application",
            state_kinds=(STATE_SERVING_REPLICA,),
            tiers=("local_memory",),
            peer_replication=False,
            gpu_state=False,
            communicator_rebuild_applicable=False,
            communicator_rebuild_s=None,
            restore_fixed_s=None,
            restore_throughput_mb_s=None,
            basis="unknown",
            notes=(
                "Engine-delegated (vLLM sleep mode, level 1): the engine offloads its weights to "
                "host RAM and discards the KV cache; its processes keep running. State lives in "
                "the node's memory and is lost with the node, so it cannot back a preemption "
                "promise. Wake time is unknown until restores have been recorded. Level 2 is "
                "not driven."
            ),
        )

    # ── transport ───────────────────────────────────────────────────────────────────────────

    def _client(self, base_url: str) -> httpx.Client:
        headers = {"Accept": "application/json"}
        key = _key_for(base_url)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=_timeout(),
            transport=self._transport,
            follow_redirects=False,
        )

    @staticmethod
    def _call(client: httpx.Client, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            resp = client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise VLLMUnreachable(f"vLLM {method} {path} failed: {exc}") from exc
        if resp.status_code == 404:
            raise SuspendUnsupported(
                f"vLLM {path} not found - start the server with --enable-sleep-mode and "
                "VLLM_SERVER_DEV_MODE=1"
            )
        if resp.status_code in (401, 403):
            raise SuspendError(
                f"vLLM {method} {path} -> HTTP {resp.status_code}: EXAMLOPS_VLLM_API_KEY is only "
                f"sent to the origin of {URL_ENV}"
            )
        if resp.status_code >= 500:
            raise VLLMUnreachable(f"vLLM {method} {path} -> HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise SuspendError(f"vLLM {method} {path} -> HTTP {resp.status_code}")
        return resp

    def _is_sleeping(self, client: httpx.Client) -> bool:
        resp = self._call(client, "GET", "/is_sleeping")
        try:
            value = resp.json().get("is_sleeping")
        except (ValueError, AttributeError) as exc:
            raise SuspendError("vLLM /is_sleeping returned no JSON object") from exc
        if not isinstance(value, bool):
            raise SuspendError(f"vLLM /is_sleeping returned {value!r}, not a boolean")
        return value

    # ── seam ────────────────────────────────────────────────────────────────────────────────

    def snapshot(
        self, subject_kind: str, subject_id: str, *, options: dict[str, Any] | None = None
    ) -> SnapshotHandle:
        self._check(self.capability(), subject_kind, options)
        raw_level = (options or {}).get("level", SUPPORTED_LEVEL)
        try:
            level = int(raw_level)
        except (TypeError, ValueError) as exc:
            raise SuspendError(f"vLLM sleep level must be an integer, got {raw_level!r}") from exc
        if level != SUPPORTED_LEVEL:
            raise SuspendUnsupported(
                f"vLLM sleep level {level} is not driven: only level {SUPPORTED_LEVEL} keeps the "
                "weights, and waking from level 2 needs a weight reload"
            )
        base_url = _base_url(options)
        with self._client(base_url) as client:
            if self._is_sleeping(client):
                raise SuspendError(
                    f"replica {subject_id!r} at {base_url} is already asleep; refusing to record "
                    "a second suspend of the same state"
                )
            self._call(client, "POST", "/sleep", params={"level": level})
            if not self._is_sleeping(client):
                raise SuspendError(f"vLLM at {base_url} accepted /sleep but is not sleeping")
        return SnapshotHandle(
            snapshot_id=uuid.uuid4().hex,
            backend=self.name,
            subject_kind=subject_kind,
            subject_id=subject_id,
            pointer={"base_url": base_url, "level": level},
            state_bytes=None,  # the engine does not report how much it offloaded
            created_at=time.time(),
        )

    def restore(self, handle: SnapshotHandle) -> RestoreReport:
        ptr = handle.pointer or {}
        try:
            base_url = _base_url({"base_url": ptr.get("base_url")})
        except SuspendError as exc:
            return RestoreReport(False, None, None, str(exc))
        try:
            with self._client(base_url) as client:
                if not self._is_sleeping(client):
                    return RestoreReport(
                        False,
                        None,
                        None,
                        "engine is not asleep (restarted or woken elsewhere); the offloaded "
                        "state this snapshot referred to no longer exists",
                    )
                t0 = time.perf_counter()
                self._call(client, "POST", "/wake_up")
                awake = not self._is_sleeping(client)
                elapsed = time.perf_counter() - t0
                if not awake:
                    return RestoreReport(
                        False, None, None, "vLLM accepted /wake_up but still sleeps"
                    )
                self._call(client, "GET", "/health")
        except VLLMUnreachable:
            raise  # transient: leave the record suspended so the resume can be retried
        except SuspendError as exc:
            return RestoreReport(False, None, None, str(exc))
        return RestoreReport(
            True,
            elapsed,
            0.0,  # processes and process groups survived the sleep; nothing was rebuilt
            f"woke the engine at {base_url} (weights moved back to the GPU; KV cache starts "
            "empty) and /health answered",
        )

    def discard(self, handle: SnapshotHandle) -> None:
        # Releasing the record does not wake the engine: an operator who discards a sleep
        # snapshot has decided not to resume through this record. The engine stays as it is.
        return None
