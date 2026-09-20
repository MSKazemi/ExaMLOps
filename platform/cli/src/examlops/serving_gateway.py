"""The serving gateway's authorization decision (plan P4.4, ADR 0126 decision 2).

Envoy fronts the serving plane; for every request it asks this service (Envoy's HTTP ``ext_authz``)
whether to let it through. The decision:

1. **Open paths** — ``GET /v2`` and ``GET /v2/health/*``, and their gRPC twins ``ServerLive``,
   ``ServerReady`` and ``ServerMetadata`` — pass without a credential, so load balancers can probe.
2. **A credential is required** for everything else: ``Authorization: Bearer <credential>``, where
   the credential is a platform virtual key (``exa-…``, :mod:`examlops.gateway`: tenant, project,
   model allow-list, budget) or an access token from the data center's IdP (:mod:`examlops.iam`,
   ADR 0120), authorized for ``serving.infer`` by the platform policy and the center's PDP.
3. **Project scope** (with ``EXAMLOPS_MULTITENANCY`` on, plan P4.9): a model that belongs to a
   project (ADR 0088) may be called only by a virtual key issued for that project, or by a token
   whose principal holds a role in it (from its claims, or a D6 relation on ``project:<name>``).
   Routes that name no model cannot be scoped and are refused; so is gRPC, which names its model
   in the message body, and the gateway never reads a body. Off, every model is open to every
   authenticated caller, as in a single-tenant install.
4. **A per-tenant quota** — the tenant's entry in the serving snapshot (``exa gateway quota set``,
   ADR 0123 decision 3), else ``EXAMLOPS_GATEWAY_TENANT_RPM`` requests per minute — through the
   shared coordinator, so every gateway replica counts against one budget.

An allowed request goes upstream with ``X-ExaMLOps-Tenant``, ``X-ExaMLOps-Principal`` and
``X-ExaMLOps-Project`` set from the verified identity. They are **always** set on an allowed request
(empty on open paths), and Envoy overwrites a client's copy with them, so a client cannot name its
own tenant.

Availability follows the serving plane's static-stability rule (ADR 0123): a virtual key that
verified recently keeps verifying for ``EXAMLOPS_GATEWAY_KEY_CACHE_SECONDS`` while the datastore is
unreachable, and a quota check that cannot reach the coordinator lets the request through (a quota
is a fairness control; the credential has already been checked). Nothing is ever allowed without a
credential.
"""

# No `from __future__ import annotations`: create_app() imports FastAPI's Request inside the
# factory (the decision logic must not need FastAPI), and FastAPI resolves string annotations
# against module globals — `request: Request` would then be read as a query parameter.
import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TENANT_HEADER = "x-examlops-tenant"
PRINCIPAL_HEADER = "x-examlops-principal"
PROJECT_HEADER = "x-examlops-project"
IDENTITY_HEADERS = (TENANT_HEADER, PRINCIPAL_HEADER, PROJECT_HEADER)

_MODEL_PATH = re.compile(r"^/(?:v2/models|predict)/([^/]+)")
_OPEN = re.compile(r"^/v2(?:/health/(?:live|ready))?/?$")
# The Open Inference Protocol over gRPC: every RPC is a POST to /<service>/<method>.
GRPC_SERVICE_PREFIX = "/inference.GRPCInferenceService/"
_GRPC_OPEN = {GRPC_SERVICE_PREFIX + m for m in ("ServerLive", "ServerReady", "ServerMetadata")}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Decision:
    """What Envoy should do: ``status`` 200 allows (with ``headers`` added upstream)."""

    status: int
    reason: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.status == 200


def model_of(path: str) -> str | None:
    """The model a request addresses, from ``/v2/models/{name}/…`` or ``/predict/{name}``."""
    m = _MODEL_PATH.match(path)
    return m.group(1) if m else None


def _identity(tenant: str, principal: str, project: str = "") -> dict[str, str]:
    return {TENANT_HEADER: tenant, PRINCIPAL_HEADER: principal, PROJECT_HEADER: project}


def _deny(status: int, reason: str, **headers: str) -> Decision:
    return Decision(status, reason, dict(headers))


# ── virtual keys, with a positive cache for datastore outages ──────────────────

# ── project scope (plan P4.9) ──────────────────────────────────────────────────

_project_cache: dict[str, tuple[float, str | None]] = {}


class _ScopeUnknown(Exception):
    """The model's project could not be read and nothing is cached: refuse rather than guess."""


def _scoping() -> bool:
    try:
        from examlops.authz import multitenancy_enabled  # noqa: PLC0415

        return multitenancy_enabled()
    except Exception:  # noqa: BLE001
        return False


def _model_project(model: str) -> str | None:
    """The project a model belongs to (``None`` = unscoped), cached for a short while.

    Read from the platform store's project membership. A read that fails serves the cached answer;
    with nothing cached it raises, so an outage can never turn a scoped model into an open one.
    """
    ttl = _env_int("EXAMLOPS_GATEWAY_SCOPE_CACHE_SECONDS", 30)
    key = model.strip().lower()
    with _key_lock:
        hit = _project_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        project = _project_of(model)
    except Exception as exc:  # noqa: BLE001
        if hit is not None:
            return hit[1]
        raise _ScopeUnknown(str(exc)) from exc
    with _key_lock:
        _project_cache[key] = (time.monotonic(), project)
    return project


def _project_of(model: str) -> str | None:
    """Project membership, matched case-insensitively. Operators assign the registry spelling
    (``JPCP``), callers address the MLflow name (``jpcp``): an exact match would find neither,
    and the model would look unscoped — open to every tenant."""
    from examlops.data import get_db, init_db  # noqa: PLC0415

    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT project FROM project_resources WHERE kind='model' AND lower(ref)=lower(?) "
            "UNION SELECT project FROM project_models WHERE lower(model)=lower(?) LIMIT 1",
            (model, model),
        ).fetchone()
    return str(row["project"]) if row else None


def _scope(model: str | None, path: str = "") -> tuple[Decision | None, str | None]:
    """(a refusal, or None) and the model's project when scoping is on."""
    if model is None:
        grpc = path.startswith(GRPC_SERVICE_PREFIX)
        return _deny(403, _SCOPED_GRPC if grpc else _SCOPED_NO_MODEL), None
    try:
        return None, _model_project(model)
    except _ScopeUnknown:
        return _deny(503, "project membership unavailable"), None


_SCOPED_NO_MODEL = (
    "with multi-tenancy on, only routes that name a model can be authorized; "
    "call /v2/models/{name}/infer"
)
_SCOPED_GRPC = (
    "with multi-tenancy on, gRPC cannot be authorized: it names the model in the message body, "
    "which the gateway does not read; call /v2/models/{name}/infer over REST"
)
_NAMES_NO_MODEL = (
    "this key is limited to models on its allow-list, and this route names none; "
    "call /v2/models/{name}/infer"
)
_key_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_key_lock = threading.Lock()


def _virtual_key(key: str, model: str | None, scoped: bool = False, path: str = "") -> Decision:
    from examlops import gateway  # noqa: PLC0415

    digest = hashlib.sha256(key.encode()).hexdigest()
    try:
        record = gateway.authorize(key, model or "")
    except gateway.KeyInvalid as exc:
        return _deny(401, str(exc), **{"www-authenticate": "Bearer"})
    except gateway.ModelNotAllowed as exc:
        if model is None:  # a route that names no model cannot be checked against an allow-list
            return _deny(403, _NAMES_NO_MODEL)
        return _deny(403, str(exc))
    except gateway.BudgetExceeded as exc:
        return _deny(403, str(exc))
    except Exception as exc:  # noqa: BLE001 - the datastore is down: last verdict, for a while
        ttl = _env_int("EXAMLOPS_GATEWAY_KEY_CACHE_SECONDS", 60)
        with _key_lock:
            cached = _key_cache.get(digest)
        if cached is None or time.monotonic() - cached[0] > ttl:
            logger.warning("Virtual key could not be verified: %s", exc)
            return _deny(503, "credential store unavailable")
        record = cached[1]
    else:
        with _key_lock:
            _key_cache[digest] = (time.monotonic(), record)
    # Enforced here as well: on the cached (datastore-outage) path gateway.authorize did not run.
    allow = record.get("models") or []
    if allow and model is None:
        return _deny(403, _NAMES_NO_MODEL)
    if allow and model not in allow:
        return _deny(403, f"key not allow-listed for model {model!r}")
    if scoped:
        refused, owner = _scope(model, path)
        if refused is not None:
            return refused
        key_project = str(record.get("project") or "")
        if owner is not None and owner != key_project:
            return _deny(
                403,
                f"model {model!r} belongs to project {owner!r}; this key is for {key_project!r}",
            )
    return Decision(
        200,
        "virtual key",
        _identity(
            str(record.get("tenant") or "default"),
            f"key:{digest[:12]}",
            str(record.get("project") or ""),
        ),
    )


def _access_token(token: str, model: str | None, scoped: bool = False, path: str = "") -> Decision:
    from examlops.iam import pdp, tokens  # noqa: PLC0415
    from examlops.iam.config import load_config  # noqa: PLC0415

    try:
        config = load_config()
    except Exception:  # noqa: BLE001 - no trust file: tokens cannot be verified
        config = None
    if config is None or not getattr(config, "providers", None):
        return _deny(
            401,
            "no identity provider is configured; use a virtual key",
            **{"www-authenticate": "Bearer"},
        )
    try:
        principal = tokens.verify_access_token(token, config)
    except tokens.AuthenticationError as exc:
        return _deny(
            401, f"invalid token: {exc}", **{"www-authenticate": 'Bearer error="invalid_token"'}
        )
    owner: str | None = None
    if scoped:
        refused, owner = _scope(model, path)
        if refused is not None:
            return refused
        if owner is not None and not _member(principal, owner, model or ""):
            return _deny(403, f"model {model!r} belongs to project {owner!r}, which you are not in")
    resource: dict[str, Any] = {"type": "model", "id": model or "*"}
    if owner is not None:
        resource["project"] = owner  # a project role counts for the policy (effective_role)
    decision = pdp.authorize(principal, "serving.infer", resource, config=config)
    if not decision.allowed:
        return _deny(403, decision.reason)
    return Decision(200, "access token", _identity(principal.tenant, principal.actor, owner or ""))


def _member(principal: Any, project: str, model: str) -> bool:
    """A role in the project: from the token's own claims, or a D6 relation on it."""
    if project in (getattr(principal, "projects", None) or {}):
        return True
    from examlops import authz  # noqa: PLC0415

    return authz.check(principal.id, "viewer", f"project:{project}/model:{model}")


# ── per-tenant quotas, from the serving snapshot (ADR 0123 decision 3) ──────────

_quota_lock = threading.Lock()
_quota_state: dict[str, Any] = {"checked": 0.0, "generation": None, "tenants": None}


def reset_quota_cache() -> None:
    """Forget the quotas read from the snapshot (tests; a process otherwise keeps them for life)."""
    with _quota_lock:
        _quota_state.update(checked=0.0, generation=None, tenants=None)


def _snapshot_quotas() -> dict[str, int]:
    """Per-tenant rpm overrides from the newest serving snapshot; ``{}`` when it carries none.

    One indexed ``MAX(generation)`` per ``EXAMLOPS_GATEWAY_QUOTA_REFRESH_SECONDS``, and the body
    is read only when a new generation exists. A snapshot whose digest does not match its content
    is refused. A failed read keeps the quotas last read — a datastore outage must neither lift a
    tenant's limit nor invent one — and a process that never read any enforces only the default.
    """
    ttl = float(_env_int("EXAMLOPS_GATEWAY_QUOTA_REFRESH_SECONDS", 5))
    now = time.monotonic()
    with _quota_lock:
        known = _quota_state["tenants"]
        if known is not None and now - _quota_state["checked"] < ttl:
            return known
        generation = _quota_state["generation"]
    try:
        from examlops import serving_snapshot  # noqa: PLC0415

        newest = serving_snapshot.latest_generation()
        if newest is None:
            fresh: dict[str, int] = {}
        elif newest == generation and known is not None:
            fresh = known
        else:
            snap = serving_snapshot.latest() or {}
            content = {k: snap[k] for k in serving_snapshot.CONTENT_KEYS if k in snap}
            if snap and serving_snapshot.digest_of(content) != snap.get("digest"):
                raise ValueError(f"serving snapshot {newest} fails its digest check")
            fresh = {
                str(t): int(v["rpm"])
                for t, v in ((snap.get("quotas") or {}).get("tenants") or {}).items()
                if isinstance(v, dict) and isinstance(v.get("rpm"), int) and v["rpm"] >= 0
            }
    except Exception as exc:  # noqa: BLE001 - keep serving with what was last known
        logger.warning("Gateway quota snapshot unavailable, keeping the last known: %s", exc)
        with _quota_lock:
            _quota_state["checked"] = now
            return _quota_state["tenants"] or {}
    with _quota_lock:
        _quota_state.update(checked=now, generation=newest, tenants=fresh)
    return fresh


def tenant_limit(tenant: str) -> int:
    """Requests per minute for ``tenant``: its snapshot quota, else the gateway default (0 = off)."""
    override = _snapshot_quotas().get(tenant)
    if override is not None:
        return override
    return _env_int("EXAMLOPS_GATEWAY_TENANT_RPM", 600)


def _within_quota(tenant: str) -> bool:
    limit = tenant_limit(tenant)
    if limit <= 0:
        return True  # 0 turns the quota off (for this tenant, or for everyone by default)
    try:
        from examlops.coordination import get_coordinator  # noqa: PLC0415

        return get_coordinator().allow(f"serving-gateway:{tenant}", limit, 60.0)
    except Exception as exc:  # noqa: BLE001 - a fairness control must not take inference down
        logger.warning("Gateway quota check unavailable, allowing: %s", exc)
        return True


def decide(method: str, path: str, authorization: str | None) -> Decision:
    """The gateway's verdict for one request (method, original path, ``Authorization`` header)."""
    if method.upper() in {"GET", "HEAD"} and _OPEN.match(path):
        return Decision(200, "open path", _identity("", ""))
    if method.upper() == "POST" and path in _GRPC_OPEN:
        return Decision(200, "open path", _identity("", ""))
    scheme, _, credential = (authorization or "").partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential:
        return _deny(401, "a bearer credential is required", **{"www-authenticate": "Bearer"})
    model = model_of(path)
    scoped = _scoping()
    if credential.startswith("exa-"):
        verdict = _virtual_key(credential, model, scoped, path)
    else:
        verdict = _access_token(credential, model, scoped, path)
    if not verdict.allowed:
        return verdict
    if not _within_quota(verdict.headers[TENANT_HEADER]):
        return _deny(429, "tenant request quota exceeded", **{"retry-after": "60"})
    return verdict


# ── the ext_authz HTTP service ────────────────────────────────────────────────

CHECK_PREFIX = "/check"
DECISION_STATUSES = (200, 401, 403, 429, 503)

# Readiness latches on the first successful credential-store read and never goes back.
#
# Both halves of that matter. Until it has read the store once, this replica cannot authorize
# anything — every request is refused 503 — so it must stay out of the Service, which is what
# holds a rolling update that shipped an unreachable store. After it has, a later outage must
# *not* un-ready it: the store is shared, so every replica would leave rotation together and
# clients would get connection errors instead of a 503 they can read. A blip is already carried
# by the verified-key cache in `decide()`.
_store_ready = False
_store_ready_lock = threading.Lock()


def reset_store_readiness() -> None:
    """Forget that the store has answered (tests; a process is otherwise latched for life)."""
    global _store_ready
    with _store_ready_lock:
        _store_ready = False


def store_reachable() -> bool:
    """Whether this process has ever read the credential store. Probes it until it has.

    The probe is the real read path with a digest no key can hash to, so it exercises exactly
    what `decide()` depends on: a reachable store answers "no such key", an unreachable one
    raises.
    """
    global _store_ready
    with _store_ready_lock:
        if _store_ready:
            return True
    from examlops.data import gateway as _data_gateway  # noqa: PLC0415

    try:
        _data_gateway.get_virtual_key("readiness-probe-no-key-hashes-to-this")
    except Exception as exc:  # noqa: BLE001 - not reachable yet; stay out of the Service
        logger.warning("Credential store not reachable yet, reporting not ready: %s", exc)
        return False
    with _store_ready_lock:
        _store_ready = True
    return True


def create_app() -> Any:
    """The ASGI app Envoy calls: ``<method> /check<original path>`` with the original headers.

    ``uvicorn --factory examlops.serving_gateway:create_app``. 200 allows; any other status is
    returned to the client with a ``{"error": …}`` body (Open Inference Protocol error shape).
    """
    from fastapi import FastAPI, Request  # noqa: PLC0415
    from fastapi.responses import JSONResponse, Response  # noqa: PLC0415

    app = FastAPI(title="ExaMLOps serving gateway authorization", docs_url=None, redoc_url=None)
    # Every status decide() returns, at 0 from the start: a status first seen at 1 would be a new
    # series, and rate() over a series with no earlier sample is 0, so an alert selecting on it
    # (ServingGatewayCredentialStoreUnavailable reads 503) would miss the first minutes of an outage.
    counts: dict[int, int] = dict.fromkeys(DECISION_STATUSES, 0)
    counts_lock = threading.Lock()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness: the process answers. Deliberately touches nothing — see `/readyz`.

        A store outage must never restart this pod: the verified-key cache is carrying traffic
        through it, and a restart throws that cache away.
        """
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> Response:
        """Readiness: this replica has read the credential store at least once.

        Until then it can only refuse (503), so it must stay out of the Service — which is what
        stops a rolling update from replacing working replicas with ones that authorize nothing.
        """
        if not store_reachable():
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/metrics")
    def metrics() -> Response:
        with counts_lock:
            lines = [
                "# HELP examlops_gateway_decisions_total Serving-gateway authorization decisions.",
                "# TYPE examlops_gateway_decisions_total counter",
                *(
                    f'examlops_gateway_decisions_total{{status="{code}"}} {n}'
                    for code, n in sorted(counts.items())
                ),
            ]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.api_route(
        CHECK_PREFIX + "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    def check(path: str, request: Request) -> Response:
        verdict = decide(request.method, "/" + path, request.headers.get("authorization"))
        with counts_lock:
            counts[verdict.status] = counts.get(verdict.status, 0) + 1
        if verdict.allowed:
            return Response(status_code=200, headers=verdict.headers)
        return JSONResponse(
            {"error": verdict.reason}, status_code=verdict.status, headers=verdict.headers
        )

    return app
