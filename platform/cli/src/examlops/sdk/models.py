"""``examlops.models`` — the model registry through the stable SDK (ADR 0078 clause 1).

Reads (``list``/``get``/``diff``/``lineage``/``cost``) wrap the MLflow registry and the
``model_costs`` table; the CLI's ``exa models list|info|diff|lineage|cost`` render these objects,
so there is one code path behind both front ends (clause 2).

Mutations (``retrain``/``approve``/``promote``) carry the CLI's discipline (Phase 29): a dry run
changes nothing; anything else needs ``confirm=True`` (the programmatic confirmation prompt);
the policy-as-code gate (ADR 0079) is consulted and a ``require_approval`` rule needs an explicit
``approved=True``; every completed mutation is written to ``audit_events``.

All heavy imports are lazy, so ``import examlops`` stays cheap and cycle-free.
"""

from __future__ import annotations

import builtins
import os
import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

from examlops.sdk.errors import (
    ApprovalRequiredError,
    ConfirmationRequiredError,
    IncompleteReadError,
    InvalidArgumentError,
    NotFoundError,
    PolicyDeniedError,
    SDKError,
    UnavailableError,
    from_client_error,
)

__all__ = [
    "ModelSummary",
    "ModelDetail",
    "ValuePair",
    "ModelDiff",
    "Lineage",
    "CostRecord",
    "RetrainResult",
    "ApproveResult",
    "PromoteResult",
    "list",
    "get",
    "diff",
    "lineage",
    "cost",
    "retrain",
    "approve",
    "promote",
]

# A model / alias / metric name that reaches a URL, a policy context or an argv. Deliberately
# excludes a leading ``-`` so a value can never be read as an option by the delegated CLI.
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$")
_METRIC = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.]{0,127}$")
_OPERATORS = ("lt", "gt", "lte", "gte")


def _check_name(value: str, what: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise InvalidArgumentError(f"invalid {what}: {value!r}")
    return value


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "sdk"


def _version_key(v: Any) -> tuple[int, Any]:
    """Numeric-where-possible ordering: MLflow versions are strings and ``"10" < "9"`` lexically."""
    try:
        return (1, int(v))
    except (TypeError, ValueError):
        return (0, str(v))


def _mlflow_get(url: str) -> Any:
    from examlops.cli import _client

    try:
        return _client.get(url)
    except _client.ClientError as exc:
        raise from_client_error(exc) from exc


# ── typed results ───────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ModelSummary:
    """One registered model: its lifecycle aliases and newest version."""

    name: str
    production_version: str | None
    latest_version: str | None
    aliases: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelDetail:
    """A registered model's aliases and versions; ``raw`` is the registry record."""

    name: str
    aliases: dict[str, str]
    versions: builtins.list[str]
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValuePair:
    """One metric or parameter as it stands in two versions (``None`` = absent there)."""

    v1: Any
    v2: Any


@dataclass(frozen=True)
class ModelDiff:
    """Metrics and parameters of two versions of one model, side by side."""

    model: str
    v1: str
    v2: str
    metrics: dict[str, ValuePair]
    params: dict[str, ValuePair]

    def to_dict(self) -> dict[str, Any]:
        """The historical ``exa --json models diff`` document."""
        return {
            "model": self.model,
            "v1": self.v1,
            "v2": self.v2,
            "metrics": {k: {"v1": p.v1, "v2": p.v2} for k, p in self.metrics.items()},
            "params": {k: {"v1": p.v1, "v2": p.v2} for k, p in self.params.items()},
        }


@dataclass(frozen=True)
class Lineage:
    """The pipeline run → dataset revision → MLflow run → model version chain of one version."""

    model: str
    model_version: str
    run_id: str
    created_ms: int
    prefect_flow_run_id: str
    dataset_version: str
    training_rows: str
    params: dict[str, Any]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """The historical ``exa --json models lineage`` document."""
        return asdict(self)


@dataclass(frozen=True)
class CostRecord:
    """One recorded HPC cost of one model version (``model_costs``)."""

    model: str
    version: int
    run_id: str | None
    job_id: str | None
    gpu_hours: float | None
    cpu_hours: float | None
    cost_usd: float | None
    recorded_at: str | None
    # ADR 0030 decision 5: the share of the device ``gpu_hours`` was billed at when the job was
    # linked to a fractional allocation (MIG slice / GPU shard), and the mechanism that provided
    # it. ``None`` = the scheduler's whole-device hours, unscaled. Defaulted, so additive.
    gpu_fraction: float | None = None
    gpu_mechanism: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetrainResult:
    """The outcome of :func:`retrain`. ``dispatched`` is False while the control plane queues it."""

    model: str
    dataset: str
    dry_run: bool
    dispatched: bool = False
    flow_run_id: str | None = None
    command_id: str | None = None
    state: str | None = None
    audited: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApproveResult:
    """The outcome of :func:`approve`."""

    model: str
    dry_run: bool
    flow_run_id: str | None = None
    status: str | None = None
    audited: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromoteResult:
    """The outcome of :func:`promote`. ``promoted`` is True only when the alias actually moved."""

    model: str
    from_alias: str
    to_alias: str
    dry_run: bool
    promoted: bool
    message: str
    warnings: builtins.list[str] = field(default_factory=builtins.list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── reads ───────────────────────────────────────────────────────────────────────────────────────
def list() -> builtins.list[ModelSummary]:  # noqa: A001 - the documented SDK name
    """Every registered model (all pages), with its Production alias and newest version.

    Raises :class:`IncompleteReadError` rather than returning a partial registry.
    """
    from examlops.cli import _client
    from examlops.cli._config import load_config
    from examlops.mlflow_paging import PagingError, all_items

    cfg = load_config()
    url = f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/search"
    try:
        models = all_items(_client.get, url, "registered_models")
    except _client.ClientError as exc:
        raise from_client_error(exc) from exc
    except PagingError as exc:
        raise IncompleteReadError(str(exc)) from exc
    out: builtins.list[ModelSummary] = []
    for m in models:
        aliases = {
            str(a["alias"]): str(a["version"])
            for a in m.get("aliases", []) or []
            if "alias" in a and "version" in a
        }
        versions = [str(v["version"]) for v in m.get("latest_versions", []) or [] if "version" in v]
        out.append(
            ModelSummary(
                name=str(m.get("name")),
                production_version=aliases.get("Production"),
                latest_version=max(versions, key=_version_key) if versions else None,
                aliases=aliases,
            )
        )
    return out


def get(name: str) -> ModelDetail:
    """One registered model's aliases and versions. :class:`NotFoundError` when it is unknown."""
    from examlops.cli._config import load_config

    cfg = load_config()
    q = urllib.parse.quote(name)
    data = _mlflow_get(f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/get?name={q}")
    rm = dict((data or {}).get("registered_model") or {})
    aliases = {
        str(a["alias"]): str(a["version"])
        for a in rm.get("aliases", []) or []
        if "alias" in a and "version" in a
    }
    versions = [str(v["version"]) for v in rm.get("latest_versions", []) or [] if "version" in v]
    return ModelDetail(name=str(rm.get("name") or name), aliases=aliases, versions=versions, raw=rm)


def _run_of(mlflow_url: str, model: str, version: str) -> tuple[str, dict[str, Any], int]:
    q = urllib.parse.quote(model)
    ver = _mlflow_get(
        f"{mlflow_url}/api/2.0/mlflow/model-versions/get?name={q}"
        f"&version={urllib.parse.quote(str(version))}"
    )
    mv = (ver or {}).get("model_version") or {}
    run_id = mv.get("run_id")
    if not run_id:
        raise NotFoundError(f"{model} v{version} has no MLflow run")
    run = _mlflow_get(f"{mlflow_url}/api/2.0/mlflow/runs/get?run_id={urllib.parse.quote(run_id)}")
    return (
        str(run_id),
        dict((run or {}).get("run", {}).get("data", {}) or {}),
        int(mv.get("creation_timestamp") or 0),
    )


def _kv(items: Any) -> dict[str, Any]:
    if isinstance(items, dict):
        return dict(items)
    return {i["key"]: i["value"] for i in items or [] if "key" in i}


def diff(name: str, v1: str, v2: str) -> ModelDiff:
    """Compare the metrics and parameters of two versions of ``name``."""
    from examlops.cli._config import load_config

    cfg = load_config()
    _, d1, _ = _run_of(cfg.mlflow_url, name, v1)
    _, d2, _ = _run_of(cfg.mlflow_url, name, v2)
    m1, m2 = _kv(d1.get("metrics")), _kv(d2.get("metrics"))
    p1, p2 = _kv(d1.get("params")), _kv(d2.get("params"))
    return ModelDiff(
        model=name,
        v1=str(v1),
        v2=str(v2),
        metrics={k: ValuePair(m1.get(k), m2.get(k)) for k in sorted(set(m1) | set(m2))},
        params={k: ValuePair(p1.get(k), p2.get(k)) for k in sorted(set(p1) | set(p2))},
    )


def lineage(name: str, version: str | None = None) -> Lineage:
    """The lineage chain of ``version`` (default: the Production alias, else any alias)."""
    from examlops.cli._config import load_config

    cfg = load_config()
    if version is None:
        detail = get(name)
        version = detail.aliases.get("Production") or next(iter(detail.aliases.values()), None)
        if not version:
            raise NotFoundError(f"No versions found for {name}")
    run_id, data, created_ms = _run_of(cfg.mlflow_url, name, str(version))
    tags = _kv(data.get("tags"))
    return Lineage(
        model=name,
        model_version=str(version),
        run_id=run_id,
        created_ms=created_ms,
        prefect_flow_run_id=str(tags.get("prefect_flow_run_id", "unknown")),
        # A1/ADR 0130 runs tag `dataset_revision`; older runs `dataset_version`.
        dataset_version=str(tags.get("dataset_revision") or tags.get("dataset_version", "unknown")),
        training_rows=str(tags.get("training_rows", "unknown")),
        params=_kv(data.get("params")),
        metrics=_kv(data.get("metrics")),
    )


def cost(name: str) -> builtins.list[CostRecord]:
    """Recorded HPC cost history of ``name``, oldest version first (read-only).

    Ingesting new scheduler accounting stays ``exa models cost --record``.
    """
    from examlops.data import init_db
    from examlops.data.finops import get_model_costs

    try:
        init_db()
        rows = get_model_costs(name)
    except Exception as exc:  # noqa: BLE001 - the datastore's own error types are private
        raise UnavailableError(f"platform datastore unavailable: {exc}") from exc
    return [
        CostRecord(
            model=str(r.get("model_name") or name),
            version=int(r["version"]),
            run_id=r.get("run_id"),
            job_id=r.get("job_id"),
            gpu_hours=r.get("gpu_hours"),
            cpu_hours=r.get("cpu_hours"),
            cost_usd=r.get("cost_usd"),
            recorded_at=r.get("recorded_at"),
            gpu_fraction=r.get("gpu_fraction"),
            gpu_mechanism=r.get("gpu_mechanism"),
        )
        for r in rows
    ]


# ── mutations ───────────────────────────────────────────────────────────────────────────────────
def _require_consent(dry_run: bool, confirm: bool, what: str) -> None:
    if not dry_run and not confirm:
        raise ConfirmationRequiredError(
            f"{what} changes platform state: pass confirm=True (or dry_run=True to preview)"
        )


def _policy_gate(
    action: str, context: dict[str, Any], approved: bool, *, audit_every: bool = True
) -> Any:
    """Consult ADR 0079 policy for ``action``; raise on deny or on an unmet approval.

    ``audit_every`` mirrors the CLI caller: ``exa retrain`` audits every decision, while the gates
    added later (``_policy_gate.enforce``) audit only a decision an operator rule made, so a site
    with no policy file keeps a byte-identical audit trail.
    """
    from examlops import policy

    decision = policy.decide_safe(action, context, default_effect=policy.DENY, audit=audit_every)
    if not audit_every and (decision.rule is not None or decision.shadow):
        policy.record_decision(action, context, decision)
    if decision.denied:
        raise PolicyDeniedError(
            f"Policy denied {action} of {context.get('model')}: {decision.reason}",
            rule=getattr(decision, "rule", None),
        )
    if decision.requires_approval and not approved:
        raise ApprovalRequiredError(
            f"Policy requires approval for {action} of {context.get('model')}: "
            f"{decision.reason} — pass approved=True once a human has approved it",
            rule=getattr(decision, "rule", None),
        )
    return decision


def retrain(
    model: str,
    dataset: str | None = None,
    *,
    dummy: bool = False,
    backend: str | None = None,
    dry_run: bool = False,
    confirm: bool = False,
    approved: bool = False,
    reason: str | None = None,
    wait: float | None = None,
    source: str = "sdk",
) -> RetrainResult:
    """Schedule a retrain through the control plane's command API (``POST /v1/retrain``).

    ``dataset`` defaults to the model's primary dataset in the use-case pack. ``wait`` bounds how
    long to wait for the dispatch (default ``EXAMLOPS_RETRAIN_WAIT_SECONDS``); a retrain still
    queued then comes back with ``dispatched=False`` — accepted, not lost.
    """
    _check_name(model, "model")
    if backend is not None:
        _check_name(backend, "backend")
    from examlops.usecase import default_dataset_for

    dataset_name = dataset or default_dataset_for(model)
    if not dataset_name:
        raise InvalidArgumentError(f"dataset is required (no default dataset in {model}'s YAML)")
    _check_name(dataset_name, "dataset")
    body: dict[str, Any] = {
        "model_name": model,
        "dataset_name": dataset_name,
        "is_dummy": bool(dummy),
        "backend_name": backend,
    }
    if dry_run:
        return RetrainResult(model=model, dataset=dataset_name, dry_run=True, raw=body)
    _require_consent(dry_run, confirm, f"retrain of {model}")
    decision = _policy_gate(
        "retrain", {"model": model, "dataset": dataset_name, "dummy": bool(dummy)}, approved
    )

    from contextlib import nullcontext

    from examlops import retrain_command
    from examlops.cli import _client
    from examlops.cli._config import load_config
    from examlops.policy import http_gate

    cfg = load_config()
    if not cfg.control_plane_token:
        raise SDKError("CONTROL_PLANE_TOKEN not configured — cannot trigger a retrain")
    ack = http_gate.approval_acknowledged() if decision.requires_approval else nullcontext()
    with ack:
        try:
            result = retrain_command.submit(
                body, base=cfg.control_plane_url, token=cfg.control_plane_token, wait=wait
            )
        except _client.ClientError as exc:
            raise from_client_error(exc) from exc

    from examlops.cli._provenance import audit_details
    from examlops.data.audit import audit_best_effort

    audited = audit_best_effort(
        source,
        _actor(),
        "retrain_triggered",
        model.upper(),
        audit_details(
            {
                "dataset": dataset_name,
                "dummy": bool(dummy),
                "backend": backend,
                "flow_run_id": result.get("flow_run_id"),
            },
            reason,
        ),
    )
    from examlops.cli.commands.retrain import _emit_retrain_lineage

    _emit_retrain_lineage(model, dataset_name, result)
    return RetrainResult(
        model=model,
        dataset=dataset_name,
        dry_run=False,
        dispatched=retrain_command.dispatched(result),
        flow_run_id=result.get("flow_run_id"),
        command_id=result.get("command_id"),
        state=result.get("state"),
        audited=bool(audited),
        raw=dict(result),
    )


def approve(
    model: str,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    approved: bool = False,
    reason: str | None = None,
    source: str = "sdk",
) -> ApproveResult:
    """Approve a pending model change in the sysadmin gate — this fires training immediately.

    Consults the ``model_approve`` policy action (ADR 0079; no rule ⇒ allow, unaudited) and
    writes a ``model_approved`` audit event.
    """
    _check_name(model, "model")
    if dry_run:
        return ApproveResult(model=model, dry_run=True)
    _require_consent(dry_run, confirm, f"approval of {model}")
    _policy_gate("model_approve", {"model": model, "actor": _actor()}, approved, audit_every=False)

    from examlops import control_plane_api
    from examlops.cli import _client
    from examlops.cli._config import load_config

    cfg = load_config()
    try:
        result = control_plane_api.approve(
            model, base=cfg.control_plane_url, token=cfg.control_plane_token
        )
    except _client.ClientError as exc:
        raise from_client_error(exc) from exc
    result = result if isinstance(result, dict) else {"result": result}

    from examlops.cli._provenance import audit_details
    from examlops.data.audit import audit_best_effort

    audited = audit_best_effort(
        source,
        _actor(),
        "model_approved",
        model,
        audit_details({"flow_run_id": result.get("flow_run_id")}, reason),
    )
    return ApproveResult(
        model=model,
        dry_run=False,
        flow_run_id=result.get("flow_run_id"),
        status=result.get("status", "scheduled"),
        audited=bool(audited),
        raw=dict(result),
    )


def promote(
    model: str,
    *,
    metric: str,
    operator: str,
    threshold: float,
    from_alias: str = "Staging",
    to_alias: str = "Production",
    dry_run: bool = False,
    confirm: bool = False,
    approved: bool = False,
    force: bool = False,
    timeout: float = 300.0,
) -> PromoteResult:
    """Move ``to_alias`` onto the ``from_alias`` version when ``metric <operator> threshold``.

    Runs the **same** code path as ``exa pipeline promote`` — the policy rule
    (``manual_promote``), the armed engine gates, the eval regression gate, the judge-calibration
    refusal, the parity, SLO, compliance, fairness and synthetic-only gates, the audit events, the
    lineage and the reproducibility bundle — by executing that command in a bounded child
    process. Re-implementing those gates here would be the duplicated logic ADR 0078 forbids.

    ``force`` overrides the built-in metric gates exactly as ``--force`` does (audited); it never
    overrides a policy deny. Raises :class:`GateRefusedError` when a gate refuses,
    :class:`PolicyDeniedError` on a policy deny.
    """
    _check_name(model, "model")
    _check_name(from_alias, "from_alias")
    _check_name(to_alias, "to_alias")
    if not isinstance(metric, str) or not _METRIC.match(metric):
        raise InvalidArgumentError(f"invalid metric: {metric!r}")
    if operator not in _OPERATORS:
        raise InvalidArgumentError(f"operator must be one of {', '.join(_OPERATORS)}")
    try:
        threshold_f = float(threshold)
    except (TypeError, ValueError) as exc:
        raise InvalidArgumentError(f"threshold must be a number: {threshold!r}") from exc
    if threshold_f != threshold_f or threshold_f in (float("inf"), float("-inf")):
        raise InvalidArgumentError("threshold must be finite")
    if not (0 < float(timeout) <= 3600):
        raise InvalidArgumentError("timeout must be in (0, 3600] seconds")
    _require_consent(dry_run, confirm, f"promotion of {model}")
    if not dry_run and not approved and _approval_rule_may_apply("manual_promote"):
        # The child runs non-interactively (--yes), where a require_approval rule would be
        # answered by the flag. Fail closed instead: the caller must state the approval.
        raise ApprovalRequiredError(
            f"a policy rule may require approval for promotion of {model} — pass approved=True "
            "once a human has approved it"
        )

    argv = [
        "pipeline",
        "promote",
        model,
        "--from",
        from_alias,
        "--to",
        to_alias,
        f"--if-{metric}-{operator}",
        repr(threshold_f),
    ]
    if dry_run:
        argv.append("--dry-run")
    if force:
        argv.append("--force")

    from examlops.sdk._cli_delegate import run_cli

    outcome = run_cli(argv, timeout=float(timeout))
    message = str(outcome.document.get("message") or outcome.document.get("error") or "")
    if outcome.exit_code != 0:
        raise outcome.as_error(message or f"exa pipeline promote exited {outcome.exit_code}")
    return PromoteResult(
        model=model,
        from_alias=from_alias,
        to_alias=to_alias,
        dry_run=dry_run,
        promoted=(not dry_run) and message.startswith("Promoted "),
        message=message,
        warnings=outcome.warnings,
    )


def _approval_rule_may_apply(action: str) -> bool:
    """True when any enforce-mode rule for ``action`` (or ``*``) has a require_approval effect."""
    from examlops import policy

    # Resolve the file NOW, as the child will when it imports `examlops.policy`: the module-level
    # `POLICY_YAML` froze the config dir at *this* process's import, so a caller that set
    # `EXAMLOPS_CONFIG_DIR`/`EXAMLOPS_DATA_DIR` afterwards would have the child enforce a
    # require_approval rule — answered by its own `--yes` — that this pre-check never saw.
    try:
        rules = policy._load_policies(policy._config_dir() / "policy.yaml")
    except Exception:  # noqa: BLE001 - an unreadable policy file must not grant anything
        return True
    for rule in rules:
        if rule.get("action", "*") not in (action, "*"):
            continue
        if str(rule.get("mode", policy.ENFORCE)).strip().lower() == policy.MONITOR:
            continue
        if str(rule.get("effect", policy.ALLOW)).strip().lower() == policy.REQUIRE_APPROVAL:
            return True
    return False
