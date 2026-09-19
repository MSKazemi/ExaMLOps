"""Agent-callable tools for ExaMLOps — pure, FastMCP-free.

Every tool is a plain typed function returning JSON-serialisable ``dict``s (never raising
for expected failures — it returns a structured ``{"error": ...}`` envelope instead, so an
LLM agent can reason about and recover from failures). The functions reuse the same
``load_config()`` + ``_client`` + ``platform_db`` code paths as the ``exa`` CLI, so the
agent surface and the human surface can never drift apart.

The :data:`REGISTRY` lists each tool together with a ``mutating`` flag. Read-only tools are
always exposed; mutating tools are only registered on the MCP server when writes are
explicitly enabled (see :mod:`examlops.mcp.server`).

This module has **no third-party dependencies** and is fully unit-testable without FastMCP.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from examlops.cli import _client
from examlops.cli._config import Config, load_config

# ── envelope helpers ──────────────────────────────────────────────────────────


def _err(message: str, **extra: Any) -> dict[str, Any]:
    # Route through the canonical SDK envelope (item 4.6). Byte-identical wire format:
    # {"ok": False, "error": message, **extra} — one error shape across all surfaces.
    from examlops.sdk import err

    return err(message, **extra).to_dict()


def _cfg() -> Config:
    return load_config()


def _version_key(v: Any) -> tuple[int, Any]:
    """Sort key for MLflow version strings: numeric where possible, else lexical.

    MLflow returns model versions as strings (``"9"``, ``"10"``). A plain ``max``
    would order them lexically and pick ``"9"`` over ``"10"``. This key sorts
    numeric versions numerically and pushes any non-numeric value to the front.
    """
    try:
        return (1, int(v))
    except (TypeError, ValueError):
        return (0, str(v))


def _get(url: str, token: str = "") -> dict[str, Any]:
    """GET returning either the parsed body or a structured error envelope."""
    try:
        data = _client.get(url, token=token)
    except _client.ClientError as exc:
        return _err(str(exc), status=getattr(exc, "status", None))
    return {"ok": True, "data": data}


# ── read-only tools ─────────────────────────────────────────────────────────


def platform_status() -> dict[str, Any]:
    """Return a health snapshot of the ExaMLOps platform.

    Reports which core services (control plane, MLflow, Prefect, Ray Serve, dashboard)
    are reachable, loaded serving models, active pipeline runs and pending approvals.
    Use this first to understand overall platform state.
    """
    from examlops.sdk import control_plane_status

    cfg = _cfg()
    try:
        # `/v1/status`, or `/status` on a control plane older than the /v1 API.
        data = control_plane_status(cfg.control_plane_url, cfg.control_plane_token or "")
    except _client.ClientError as exc:
        return _err(str(exc), status=getattr(exc, "status", None))
    return {"ok": True, **data}


def list_models() -> dict[str, Any]:
    """List every registered model with its Production alias, latest version and aliases.

    Reads the MLflow model registry. Model names are lowercase in the registry
    (e.g. ``jpcp``).
    """
    from examlops.mlflow_paging import PagingError, all_items

    cfg = _cfg()
    url = f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/search"
    # An agent cannot sanity-check a short list, so a partial registry is worse here than
    # elsewhere: "these are the models" is taken at face value.
    try:
        models = all_items(_client.get, url, "registered_models")
    except _client.ClientError as exc:
        return _err(str(exc), status=getattr(exc, "status", None))
    except PagingError as exc:
        return _err(str(exc))
    out = []
    for m in models:
        aliases = {
            a["alias"]: a["version"]
            for a in m.get("aliases", [])
            if "alias" in a and "version" in a
        }
        # MLflow serialises ``version`` as a string; compare numerically so that
        # e.g. version "10" sorts above "9" instead of below it.
        versions = [v["version"] for v in m.get("latest_versions", []) if "version" in v]
        latest = max(versions, key=_version_key, default=None)
        out.append(
            {
                "name": m.get("name"),
                "production": aliases.get("Production"),
                "latest": latest,
                "aliases": aliases,
            }
        )
    return {"ok": True, "models": out, "count": len(out)}


def model_detail(name: str) -> dict[str, Any]:
    """Return full detail for one registered model: versions, aliases and metrics.

    Args:
        name: Registered model name, lowercase (e.g. ``jpcp``).
    """
    cfg = _cfg()
    q = urllib.parse.quote(name)
    res = _get(f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/get?name={q}")
    if not res.get("ok"):
        return res
    return {"ok": True, "model": res["data"].get("registered_model", {})}


def list_production_models() -> dict[str, Any]:
    """Registry of model names and their training datasets — NOT which models are in production.

    The name is a misnomer kept for wire compatibility. This calls the control plane's ``/models``,
    which is the auto-discovery registry: every known model with the datasets it trains on, and no
    version or lifecycle alias at all. An agent asked "which models are in production?" must call
    ``list_models`` instead, which reads the MLflow registry and reports each model's ``Production``
    alias.
    """
    cfg = _cfg()
    res = _get(f"{cfg.control_plane_url}/v1/models", token=cfg.control_plane_token)
    if not res.get("ok"):
        return res
    return {"ok": True, **_as_dict(res["data"])}


def list_approvals() -> dict[str, Any]:
    """List pending sysadmin approval requests in the promotion gate."""
    cfg = _cfg()
    res = _get(f"{cfg.control_plane_url}/v1/approvals", token=cfg.control_plane_token)
    if not res.get("ok"):
        return res
    return {"ok": True, **_as_dict(res["data"])}


def modelzoo_status() -> dict[str, Any]:
    """Report ModelZoo repository freshness and the latest upstream events."""
    cfg = _cfg()
    res = _get(f"{cfg.control_plane_url}/v1/modelzoo/status", token=cfg.control_plane_token)
    if not res.get("ok"):
        return res
    return {"ok": True, **_as_dict(res["data"])}


def recent_audit_events(limit: int = 20, model: str | None = None) -> dict[str, Any]:
    """Return recent platform audit events (mutations, retrains, promotions, drift).

    Args:
        limit: Maximum number of events to return (most recent first). Default 20.
        model: Optional case-insensitive filter on the event target (model name).
    """
    try:
        from examlops.data import get_db, init_db

        init_db()
        limit = max(1, min(int(limit), 500))
        sql = "SELECT ts, source, actor, action, target, details FROM audit_events"
        params: list[Any] = []
        if model:
            sql += " WHERE target LIKE ?"
            params.append(f"%{model}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with get_db() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"audit query failed: {exc}")
    events = [
        {
            "ts": r[0],
            "source": r[1],
            "actor": r[2],
            "action": r[3],
            "target": r[4],
            "details": r[5],
        }
        for r in rows
    ]
    return {"ok": True, "events": events, "count": len(events)}


def _as_dict(data: Any) -> dict[str, Any]:
    """Normalise a list-or-dict API body into a dict envelope for tool output."""
    if isinstance(data, dict):
        return data
    return {"items": data}


def _db_read(query: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a platform.db read, wrapping the result in an ``ok`` envelope.

    ``query`` returns the payload dict (already keyed). Any failure — including
    ``platform.db`` being unavailable — degrades to a structured error envelope rather
    than raising, so an agent can reason about it. ``init_db()`` is idempotent (schema-once
    sentinel) so calling it per read is cheap.
    """
    try:
        from examlops.data import init_db

        init_db()
        return {"ok": True, **query()}
    except Exception as exc:  # noqa: BLE001 - reads never raise to the agent
        return _err(str(exc))


_SENSITIVE_KEYS = ("secret", "token", "password", "raw_key", "plaintext")


def _redact(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop any obviously-sensitive field before returning rows to an agent."""
    return [
        {k: v for k, v in row.items() if not any(s in k.lower() for s in _SENSITIVE_KEYS)}
        for row in rows
    ]


# ── read-only tools: Monitoring & Drift ───────────────────────────────────────


def drift_status(model: str) -> dict[str, Any]:
    """Prediction-drift status for a model: stored baseline + recent drift events.

    Reads ``drift_baselines`` + ``drift_events``. Compare live values against the baseline to
    judge drift severity; this returns the *recorded* state, not a fresh computation.
    """

    def _q() -> dict[str, Any]:
        from examlops.data.drift import get_drift_baseline, list_drift_events

        return {
            "baseline": get_drift_baseline(model),
            "events": list_drift_events(model=model, drift_kind="prediction", last_n=10),
        }

    return _db_read(_q)


def input_drift_status(model: str) -> dict[str, Any]:
    """Input-embedding drift status: stored input baseline + recent input drift events."""

    def _q() -> dict[str, Any]:
        from examlops.data.drift import get_input_baseline, list_drift_events

        return {
            "baseline": get_input_baseline(model),
            "events": list_drift_events(model=model, drift_kind="input", last_n=10),
        }

    return _db_read(_q)


def list_drift(model: str | None = None, kind: str | None = None) -> dict[str, Any]:
    """Recent unified drift events (newest first), optionally filtered by model and kind.

    Args:
        model: Optional model filter.
        kind: Optional drift kind — ``prediction``, ``input`` or ``concept``.
    """

    def _q() -> dict[str, Any]:
        from examlops.data.drift import list_drift_events

        return {"events": list_drift_events(model=model, drift_kind=kind, last_n=50)}

    return _db_read(_q)


def get_drift_autoretrain(model: str | None = None) -> dict[str, Any]:
    """Drift-triggered auto-retrain configuration (per model, or all when model omitted)."""

    def _q() -> dict[str, Any]:
        from examlops.data.drift import get_drift_auto_retrain, list_drift_auto_retrain

        if model:
            return {"config": get_drift_auto_retrain(model)}
        return {"configs": list_drift_auto_retrain()}

    return _db_read(_q)


# ── read-only tools: Serving, Traffic & Promotion ─────────────────────────────


def traffic_rules(model: str) -> dict[str, Any]:
    """Current traffic-split rules (production/canary percentages) for a model."""

    def _q() -> dict[str, Any]:
        from examlops.data.serving import get_traffic_rules

        return {"rules": get_traffic_rules(model)}

    return _db_read(_q)


def promotion_rule(model: str) -> dict[str, Any]:
    """The metric-gated promotion rule configured for a model (if any)."""

    def _q() -> dict[str, Any]:
        from examlops.data.serving import get_promotion_rule

        return {"rule": get_promotion_rule(model)}

    return _db_read(_q)


def challenger_config(model: str) -> dict[str, Any]:
    """Champion/challenger configuration for a model (shadow-eval settings)."""

    def _q() -> dict[str, Any]:
        from examlops.data.serving import get_challenger_config

        return {"config": get_challenger_config(model)}

    return _db_read(_q)


def autoscale_config(model: str) -> dict[str, Any]:
    """Autoscale / scale-to-zero configuration for a serving model."""

    def _q() -> dict[str, Any]:
        from examlops.data.serving import get_autoscale_config

        return {"config": get_autoscale_config(model)}

    return _db_read(_q)


def scale_events(model: str) -> dict[str, Any]:
    """Recent autoscale scale-up/scale-down events for a model (newest first)."""

    def _q() -> dict[str, Any]:
        from examlops.data.serving import list_scale_events

        return {"events": list_scale_events(model, last_n=50)}

    return _db_read(_q)


# ── read-only tools: SLO, Fairness & Governance ───────────────────────────────


def slo_specs(model: str | None = None) -> dict[str, Any]:
    """Configured SLO specifications (objectives) for a model, or all models."""

    def _q() -> dict[str, Any]:
        from examlops.data.governance import list_slo_specs

        return {"specs": list_slo_specs(model=model)}

    return _db_read(_q)


def slo_ratio(model: str, name: str) -> dict[str, Any]:
    """The observed good/total SLI ratio for a named SLO on a model (SLO attainment)."""

    def _q() -> dict[str, Any]:
        from examlops.data.governance import slo_sli_ratio

        good, total = slo_sli_ratio(model, name)
        ratio = (good / total) if total else None
        return {"good": good, "total": total, "ratio": ratio}

    return _db_read(_q)


def fairness_status(model: str) -> dict[str, Any]:
    """Fairness gates + fairness configuration for a model (bias/parity guardrails)."""

    def _q() -> dict[str, Any]:
        from examlops.data.finops import get_fairness_gates
        from examlops.data.governance import get_fairness_config

        return {
            "gates": get_fairness_gates(model),
            "config": get_fairness_config(model),
        }

    return _db_read(_q)


def compliance_system(model: str) -> dict[str, Any]:
    """The compliance/risk classification recorded for a model (e.g. EU-AI-Act system)."""

    def _q() -> dict[str, Any]:
        from examlops.data.governance import get_compliance_system, list_technical_files

        return {
            "system": get_compliance_system(model),
            "technical_files": list_technical_files(model),
        }

    return _db_read(_q)


def authz_relations(subject: str | None = None, obj: str | None = None) -> dict[str, Any]:
    """Relationship-based access-control grants (who has which role on which object)."""

    def _q() -> dict[str, Any]:
        from examlops.data.governance import list_relations

        return {"relations": list_relations(subject=subject, obj=obj)}

    return _db_read(_q)


# ── read-only tools: FinOps & Green-AI ────────────────────────────────────────


def model_costs(model: str) -> dict[str, Any]:
    """Recorded HPC GPU-hour / USD cost history for a model."""

    def _q() -> dict[str, Any]:
        from examlops.data.finops import get_model_costs

        return {"costs": get_model_costs(model)}

    return _db_read(_q)


def carbon(model: str | None = None) -> dict[str, Any]:
    """Green-AI carbon-accounting records (kgCO2e) for a model, or the whole platform."""

    def _q() -> dict[str, Any]:
        from examlops.data.finops import get_carbon_records

        return {"records": get_carbon_records(model)}

    return _db_read(_q)


def platform_cost_summary() -> dict[str, Any]:
    """Aggregated per-model cost rollup across the platform (FinOps overview)."""

    def _q() -> dict[str, Any]:
        from examlops.data.finops import aggregate_model_costs

        return {"summary": aggregate_model_costs()}

    return _db_read(_q)


def gateway_cost() -> dict[str, Any]:
    """Total LLM-gateway spend (USD) across virtual keys."""

    def _q() -> dict[str, Any]:
        from examlops.data.finops import total_gateway_cost

        return {"total_usd": total_gateway_cost()}

    return _db_read(_q)


# ── read-only tools: Gateway & LLMOps ─────────────────────────────────────────


def gateway_config(model: str) -> dict[str, Any]:
    """The LLM-gateway routing configuration for a model (backends, fallbacks)."""

    def _q() -> dict[str, Any]:
        from examlops.data.gateway import get_gateway_config

        return {"config": get_gateway_config(model)}

    return _db_read(_q)


def gateway_cache_stats() -> dict[str, Any]:
    """Semantic-cache hit-rate and savings for the LLM gateway."""

    def _q() -> dict[str, Any]:
        from examlops.data.gateway import cache_stats

        return {"stats": cache_stats()}

    return _db_read(_q)


def list_gateway_keys() -> dict[str, Any]:
    """List LLM-gateway virtual keys (metadata only — secret material is never returned)."""

    def _q() -> dict[str, Any]:
        from examlops.data.gateway import list_virtual_keys

        return {"keys": _redact(list_virtual_keys())}

    return _db_read(_q)


# ── read-only tools: Evaluation & Data ────────────────────────────────────────


def eval_gate(model: str) -> dict[str, Any]:
    """The evaluation regression-gate configuration for a model (thresholds/suites)."""

    def _q() -> dict[str, Any]:
        from examlops.data.evaluation import get_eval_gate

        return {"gate": get_eval_gate(model)}

    return _db_read(_q)


def eval_results(model: str) -> dict[str, Any]:
    """Recorded evaluation-suite results for a model (newest first)."""

    def _q() -> dict[str, Any]:
        from examlops.data.evaluation import get_eval_results

        return {"results": get_eval_results(model)}

    return _db_read(_q)


def gate_reports(model: str) -> dict[str, Any]:
    """Recent regression-gate pass/fail reports for a model."""

    def _q() -> dict[str, Any]:
        from examlops.data.evaluation import get_gate_reports

        return {"reports": get_gate_reports(model)}

    return _db_read(_q)


def dataset_revisions(dataset: str) -> dict[str, Any]:
    """Recorded immutable dataset revisions for a dataset (data-versioning history)."""

    def _q() -> dict[str, Any]:
        from examlops.data.data_assets import get_dataset_revisions

        return {"revisions": get_dataset_revisions(dataset)}

    return _db_read(_q)


def dataplane_sources(project: str = "") -> dict[str, Any]:
    """Registered dataplane sources (what external data the platform can pull; no credentials)."""

    def _q() -> dict[str, Any]:
        from examlops.data.dataplane import list_sources

        rows = list_sources(project or None)
        return {
            "sources": [
                {k: r[k] for k in ("project", "name", "connector", "connection", "schedule")}
                for r in rows
            ]
        }

    return _db_read(_q)


def dataplane_snapshots(name: str, project: str = "") -> dict[str, Any]:
    """Committed snapshots of one dataplane source, newest first (revision, rows, time)."""

    def _q() -> dict[str, Any]:
        from examlops.data.dataplane import list_snapshots

        # Selected as snapshots in SQL: filtering a page of *pulls* down to the committed ones
        # answers "snapshots among the newest 50 pulls", so a source in a failure run reports
        # having none — the answer an agent is least able to sanity-check.
        return {
            "snapshots": [
                {
                    "revision": r["revision"],
                    "rows": r.get("row_count"),
                    "finished_at": r.get("finished_at"),
                }
                for r in list_snapshots(project=project, source=name, limit=50)
            ]
        }

    return _db_read(_q)


def dataplane_pull(name: str, project: str = "") -> dict[str, Any]:
    """Pull a dataplane source now and commit a snapshot. Mutating; tier A."""
    gate = _agent_write_gate("dataplane_pull", {"name": name, "project": project})
    if gate is not None:
        return gate
    from examlops.dataplane.safety import redact

    try:
        from examlops.dataplane import run_pull

        r = run_pull(name, project=project, trigger_kind="api", actor="mcp")
    except Exception as exc:  # noqa: BLE001 - failures become a safe tool response
        return _err(redact(str(exc)))
    return _with_audit(
        {"ok": True, "status": r.status, "revision": r.revision, "rows": r.row_count},
        "dataplane_pull_requested",
        name,
        {"project": project},
    )


def data_quality(dataset: str) -> dict[str, Any]:
    """Recent data-quality / contract validation checks for a dataset."""

    def _q() -> dict[str, Any]:
        from examlops.data.data_assets import get_data_quality_checks

        return {"checks": get_data_quality_checks(dataset)}

    return _db_read(_q)


def feature_view(name: str) -> dict[str, Any]:
    """Definition + last materialization of a feature-store view."""

    def _q() -> dict[str, Any]:
        from examlops.data.data_assets import get_feature_view, last_materialization

        return {"view": get_feature_view(name), "last_materialization": last_materialization(name)}

    return _db_read(_q)


# ── read-only tools: Incident & Lineage ───────────────────────────────────────


def model_lineage(model: str) -> dict[str, Any]:
    """Lineage graph for a model: the pipeline → dataset → model chain (OpenLineage)."""

    def _q() -> dict[str, Any]:
        from examlops.data.events import lineage_graph

        return {"lineage": lineage_graph(model)}

    return _db_read(_q)


def lineage_impact(dataset_revision: str) -> dict[str, Any]:
    """Downstream impact of a dataset revision: everything derived from it (blast radius)."""

    def _q() -> dict[str, Any]:
        from examlops.data.events import lineage_impact as _impact

        return {"impacted": _impact(dataset_revision)}

    return _db_read(_q)


# ── read-only tools: Help & Discoverability ───────────────────────────────────


def explain_command(command: str = "") -> dict[str, Any]:
    """Explain what an ``exa`` command does, with its copy-paste examples (grounded help).

    Introspects the live ``exa`` Typer/Click command tree, so the answer can never drift from
    the actual CLI. Pass a space-separated command path (``"drift"``, ``"serve reload"``) to
    describe that command; pass an empty string to list the top-level commands. This is the
    agent's authoritative "how do I …?" tool — prefer it over guessing command syntax.
    """
    try:
        from examlops.cli.commands.explain_command import (
            _clean,
            _extract_examples,
            _normalize,
            _resolve,
        )

        # Normalize before labelling as well as before resolving, or the echoed `command` comes
        # back as "exa exa status" — the same class of mismatch that made this tool reject its
        # own output.
        path = _normalize(command.split())
        node = _resolve(path)
        if node is None:
            return _err(f"unknown command: {command!r}")
        commands = getattr(node, "commands", None)
        label = " ".join(["exa", *path]) if path else "exa"
        summary = _clean(
            node.get_short_help_str() or (getattr(node, "help", "") or "").split("\n")[0]
        )
        out: dict[str, Any] = {"ok": True, "command": label, "summary": summary}
        if commands:
            out["subcommands"] = {
                name: _clean(sub.get_short_help_str() or (sub.help or "").split("\n")[0])
                for name, sub in sorted(commands.items())
            }
        out["examples"] = _extract_examples(getattr(node, "epilog", None))
        return out
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


# ── mutating tools (gated behind EXAMLOPS_MCP_ALLOW_WRITES) ────────────────────


def _agent_write_gate(action_kind: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """Least-privilege policy check for an agent write (ADR 0082 layer 2 + ADR 0079).

    Beyond the coarse ``EXAMLOPS_MCP_ALLOW_WRITES`` switch, each mutating tool call is checked
    against the ``agent_write`` policy. Returns an error dict when the write is disallowed, else
    ``None``. ``require_approval`` is treated as *disallowed for an agent* — there is no human at the
    tool-call boundary, so an approval-required action must not proceed autonomously. Policy being
    unavailable blocks the write (``decide_safe``, default-deny) — a coarse feature flag is not an
    authorization decision, and an agent must never gain permission because policy evaluation
    failed. Unlike a plain ``except``, the unavailable case is itself durably audited (BL-080) —
    this was the one call site that already failed closed but still only ever returned an error
    string, with no record in ``audit_events`` that policy had gone dark for an agent tool call.
    """
    from examlops import policy

    decision = policy.decide_safe(
        "agent_write", {"action_kind": action_kind, **context}, default_effect=policy.DENY
    )
    if decision.unavailable:
        return _err(f"policy unavailable; refusing agent write ({action_kind})")
    if decision.denied:
        return _err(f"policy denied agent write ({action_kind}): {decision.reason}")
    if decision.requires_approval:
        return _err(
            f"policy requires human approval for {action_kind} — not permitted to an agent "
            f"({decision.reason})"
        )
    return None


def trigger_retrain(
    model_name: str,
    dataset_name: str,
    dummy: bool = False,
    backend_name: str | None = None,
) -> dict[str, Any]:
    """Trigger a training/retraining run for a model via the control plane.

    This is a mutating action — it launches a Prefect flow run. Requires
    CONTROL_PLANE_TOKEN to be configured. Returns the flow run id and a status URL
    the agent can poll with the retrain_status tool.

    Args:
        model_name: Registered model name (e.g. ``JPCP``).
        dataset_name: Dataset class name (e.g. ``PM100Dataset``).
        dummy: If true, run a fast dummy training loop instead of full training.
        backend_name: Optional dataset backend override (zenodo/minio/dataplane).
    """
    gate = _agent_write_gate("retrain", {"model": model_name})
    if gate is not None:
        return gate
    cfg = _cfg()
    if not cfg.control_plane_token:
        return _err("CONTROL_PLANE_TOKEN not configured — cannot trigger retrain")
    body: dict[str, Any] = {
        "model_name": model_name,
        "dataset_name": dataset_name,
        "is_dummy": bool(dummy),
    }
    if backend_name:
        body["backend_name"] = backend_name
    from examlops import retrain_command  # noqa: PLC0415

    try:
        # The command API, waited on until dispatched (plan P1.6c). A retrain still queued when
        # the wait ends comes back with `command_id`/`state` instead of a `flow_run_id`.
        data = retrain_command.submit(
            body, base=cfg.control_plane_url, token=cfg.control_plane_token
        )
    except _client.ClientError as exc:
        return _err(str(exc), status=getattr(exc, "status", None))

    # The retrain is already running; auditing can no longer be allowed to fail it, but a
    # retrain nobody can trace is still worth saying out loud. Same contract as every other
    # mutating tool here.
    return _with_audit(
        {"ok": True, **_as_dict(data)},
        "retrain_triggered",
        model_name,
        {"dataset": dataset_name, "dummy": bool(dummy), "backend": backend_name},
    )


def retrain_status(flow_run_id: str) -> dict[str, Any]:
    """Poll the status of a retraining flow run started by the trigger_retrain tool.

    Args:
        flow_run_id: The flow run id returned by ``trigger_retrain``.
    """
    cfg = _cfg()
    q = urllib.parse.quote(flow_run_id)
    res = _get(f"{cfg.control_plane_url}/v1/runs/{q}", token=cfg.control_plane_token)
    if not res.get("ok"):
        return res
    return {"ok": True, **_as_dict(res["data"])}


# ── registry ──────────────────────────────────────────────────────────────────


# ── HPC fleet tools (Phase 35d) ───────────────────────────────────────────────


def hpc_clusters() -> dict[str, Any]:
    """List registered HPC clusters and their approval state (PENDING/ACTIVE/REJECTED).

    Reads the fleet registry (clusters.yaml + hpc_clusters). Use before scheduling to see
    which clusters a sysadmin has approved for training runs.
    """
    try:
        from examlops.hpc_registry import list_clusters

        return {"ok": True, "clusters": list_clusters()}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def hpc_nodes(cluster: str | None = None) -> dict[str, Any]:
    """Return the latest discovered node inventory (CPUs/memory/GPUs/state) for a cluster.

    Reads the ``hpc_nodes`` snapshot table. Refresh snapshots with
    ``exa hpc nodes --save --cluster <name>``.
    """
    try:
        from examlops.data import init_db
        from examlops.data.hpc import get_node_snapshot

        init_db()
        return {"ok": True, "nodes": get_node_snapshot(cluster)}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def hpc_place(gpus: int = 0, nodes: int = 1) -> dict[str, Any]:
    """Recommend which ACTIVE cluster should run a job needing ``gpus``/``nodes``.

    Returns the chosen cluster, a human-readable reason, and the scored candidate list. Does
    not submit anything — it only advises placement.
    """
    try:
        from examlops.hpc_placement import ResourceAsk, choose_cluster
        from examlops.hpc_placement_providers import resolve_placement_score_fn
        from examlops.hpc_registry import active_clusters_with_inventory

        result = choose_cluster(
            ResourceAsk(gpus=gpus, nodes=nodes),
            active_clusters_with_inventory(),
            resolve_placement_score_fn(),
        )
        return {"ok": True, **result.to_dict()}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def fleet_simulate(
    jobs: int = 0, gpus: int = 1, nodes: int = 1, optimize: str | None = None
) -> dict[str, Any]:
    """Run a Fleet Digital Twin what-if: project submitting ``jobs`` × ``gpus``-GPU jobs on the fleet.

    Returns the projected placements, GPU-hours, cost, carbon, and queue depth vs the current baseline
    (item 5.1) — the read-only planning tool a fleet copilot (item 5.5) composes into previewed plans.
    Touches nothing live. ``optimize`` picks a placement provider (carbon-aware/cost-aware/...).
    """
    try:
        from examlops.fleet_twin import JobSpec, Scenario, simulate
        from examlops.hpc_placement_providers import resolve_placement_score_fn

        scenario = Scenario(jobs=[JobSpec(gpus=gpus, nodes=nodes, count=jobs)] if jobs else [])
        score_fn = resolve_placement_score_fn(optimize) if optimize else None
        return {"ok": True, **simulate(scenario, score_fn=score_fn)}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def hpc_jobs(model: str | None = None) -> dict[str, Any]:
    """List tracked HPC job submissions (from the ``hpc_jobs`` table), newest first."""
    try:
        from examlops.data import init_db
        from examlops.data.hpc import get_hpc_jobs

        init_db()
        return {"ok": True, "jobs": get_hpc_jobs(model)[:50]}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def hpc_approve_cluster(name: str) -> dict[str, Any]:
    """Approve a PENDING HPC cluster so exaMLOps may schedule jobs on it (mutating, audited).

    This is the sysadmin approval gate. Only registered when writes are explicitly enabled.
    """
    gate = _agent_write_gate("approve_cluster", {"target": name})
    if gate is not None:
        return gate
    try:
        from examlops.data.hpc import set_cluster_state
        from examlops.hpc_registry import get_merged

        if get_merged(name) is None:
            return _err(f"unknown cluster: {name}")
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "mcp-agent"
        set_cluster_state(name, "ACTIVE", approved_by=actor)
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    # The cluster is ACTIVE from here on — jobs can be scheduled on it. Auditing after the
    # fact, outside that try, so a broken audit chain cannot report an approval that has
    # already taken effect as a failure the operator will try again.
    out: dict[str, Any] = {"ok": True, "cluster": name, "state": "ACTIVE"}
    warning = _audit_write("cluster_approved", name, {})
    if warning:
        out["audit_warning"] = warning
    return out


# ── Projects & Workspaces tools (ADR 0086–0090) ───────────────────────────────


def project_list() -> dict[str, Any]:
    """List ExaMLOps Projects (workspaces) with their status and resource quota.

    A Project groups models/pipelines/serving/connections + members (owner/editor/viewer).
    Reads the ``projects`` table.
    """
    try:
        from examlops.data import init_db
        from examlops.data.projects import list_projects

        init_db()
        return {"ok": True, "projects": list_projects()}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def project_detail(name: str) -> dict[str, Any]:
    """Full anatomy of one Project: quota, resources by kind, members, budget, consumption."""
    try:
        from examlops.data import init_db
        from examlops.data.projects import get_project_full

        init_db()
        full = get_project_full(name)
        if full is None:
            return _err(f"project not found: {name}")
        return {"ok": True, "project": full}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def project_cost(name: str) -> dict[str, Any]:
    """Per-project cost attribution (GPU-hours, USD, carbon) and budget/quota breach status."""
    try:
        from examlops.data import init_db
        from examlops.data.projects import get_project
        from examlops.project_finops import budget_status, cost_summary

        init_db()
        if get_project(name) is None:
            return _err(f"project not found: {name}")
        return {"ok": True, "cost": cost_summary(name), "budget": budget_status(name)}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def project_assign_model(project: str, model: str) -> dict[str, Any]:
    """Assign a model to a Project (mutating, audited). Only registered when writes are enabled."""
    gate = _agent_write_gate("project_assign_model", {"project": project, "model": model})
    if gate is not None:
        return gate
    try:
        from examlops.data import init_db
        from examlops.data.projects import assign_resource_to_project

        init_db()
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "mcp-agent"
        if not assign_resource_to_project(project, "model", model, added_by=actor):
            return _err(f"project not found: {project}")
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    out: dict[str, Any] = {"ok": True, "project": project, "model": model}
    warning = _audit_write("project_model_assigned", model, {"project": project})
    if warning:
        out["audit_warning"] = warning
    return out


def project_add_member(project: str, subject: str, role: str = "viewer") -> dict[str, Any]:
    """Add a person to a Project with an owner/editor/viewer role (mutating, audited)."""
    gate = _agent_write_gate("project_add_member", {"project": project, "subject": subject})
    if gate is not None:
        return gate
    if role not in {"owner", "editor", "viewer"}:
        return _err("role must be one of: owner, editor, viewer")
    try:
        from examlops.data import init_db
        from examlops.data.projects import add_project_member, get_project

        init_db()
        if get_project(project) is None:
            return _err(f"project not found: {project}")
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "mcp-agent"
        add_project_member(project, subject, role, actor=actor)
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    out: dict[str, Any] = {"ok": True, "project": project, "subject": subject, "role": role}
    warning = _audit_write("project_member_added", subject, {"project": project, "role": role})
    if warning:
        out["audit_warning"] = warning
    return out


# Recognised lifecycle use cases a tool serves. Drives the capabilities catalogue
# (``capabilities_catalogue``), the A2A card grouping, and the Skipper skill-router packs.
USE_CASES: tuple[str, ...] = (
    "management",
    "monitoring",
    "help",
    "incident",
    "finops",
    "governance",
)

# Write-privilege tiers (ADR 0102). ``read`` = no mutation. ``A`` = low-risk, autopilot-OK.
# ``B`` = requires human-in-the-loop confirmation. ``C`` = human-CLI-only (never bound to an
# autonomous agent). Read tools always carry ``read``.
TIERS: tuple[str, ...] = ("read", "A", "B", "C")


# ── mutating tools: configuration writes (Phase 5, ADR 0102) ──────────────────


def _with_audit(
    out: dict[str, Any], action: str, target: str, details: dict[str, Any]
) -> dict[str, Any]:
    """Audit a write that has already happened, attaching a warning if it could not be."""
    warning = _audit_write(action, target, details)
    if warning:
        out["audit_warning"] = warning
    return out


def _audit_write(action: str, target: str, details: dict[str, Any]) -> str | None:
    """Audit an agent-initiated write. Never raises; returns why it failed, or ``None``.

    Two things must both be true and they pull in opposite directions: an audit failure must
    not fail an action that has *already happened* (reporting a completed write as an error
    is a lie, and the caller will retry it), and an unaudited governance write must not be
    reported as a plain success either. So this never raises, and hands the reason back for
    the caller to surface as a warning alongside ``ok: True``.
    """
    try:
        from examlops.data import init_db
        from examlops.data.audit import write_audit_event

        init_db()
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "mcp-agent"
        write_audit_event("mcp", actor, action, target, {**details, "via": "mcp"})
    except Exception as exc:  # noqa: BLE001 - auditing never fails the write
        return f"action succeeded but was not audited: {exc}"
    return None


def set_traffic_split(model: str, production: int, canary: int = 0) -> dict[str, Any]:
    """Set the traffic-split rule for a model (production/canary %). Mutating; tier A."""
    gate = _agent_write_gate("set_traffic_split", {"model": model})
    if gate is not None:
        return gate
    if production + canary != 100:
        return _err("production + canary must sum to 100")
    try:
        from examlops.data.serving import set_traffic_rules

        set_traffic_rules(
            model, {"production": production, "canary": canary}, updated_by="mcp-agent"
        )
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    return _with_audit(
        {"ok": True, "model": model, "production": production, "canary": canary},
        "traffic_split_set",
        model,
        {"production": production, "canary": canary},
    )


def set_drift_autoretrain(
    model: str, dataset: str, enabled: bool = True, min_z: float = 3.0
) -> dict[str, Any]:
    """Configure drift-triggered auto-retrain for a model. Mutating; tier B (confirm)."""
    gate = _agent_write_gate("set_drift_autoretrain", {"model": model})
    if gate is not None:
        return gate
    try:
        from examlops.data.drift import set_drift_auto_retrain

        set_drift_auto_retrain(model, enabled, min_z_score=min_z, dataset_name=dataset)
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    return _with_audit(
        {"ok": True, "model": model, "enabled": enabled, "dataset": dataset},
        "drift_autoretrain_set",
        model,
        {"dataset": dataset, "enabled": enabled},
    )


def set_promotion_rule(model: str, metric: str, operator: str, threshold: float) -> dict[str, Any]:
    """Set the metric-gated promotion rule for a model (e.g. rmse < 5.0). Mutating; tier B."""
    gate = _agent_write_gate("set_promotion_rule", {"model": model})
    if gate is not None:
        return gate
    if operator not in {"<", "<=", ">", ">=", "=="}:
        return _err("operator must be one of < <= > >= ==")
    try:
        from examlops.data.serving import set_promotion_rule as _set

        _set(model, metric, operator, float(threshold))
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    return _with_audit(
        {"ok": True, "model": model, "rule": f"{metric} {operator} {threshold}"},
        "promotion_rule_set",
        model,
        {"metric": metric, "op": operator, "threshold": threshold},
    )


def disable_challenger(model: str) -> dict[str, Any]:
    """Disable the champion/challenger shadow eval for a model. Mutating; tier A."""
    gate = _agent_write_gate("disable_challenger", {"model": model})
    if gate is not None:
        return gate
    try:
        from examlops.data.serving import disable_challenger as _disable

        _disable(model, updated_by="mcp-agent")
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    return _with_audit(
        {"ok": True, "model": model, "challenger": "disabled"}, "challenger_disabled", model, {}
    )


def grant_access(subject: str, relation: str, obj: str) -> dict[str, Any]:
    """Grant an access relation (owner/editor/viewer) on an object. Mutating; tier C (human-only).

    Tier C means this is registered for the human `exa mcp serve` surface but is never bound to an
    autonomous agent (the Skipper bridge filters tier C out).
    """
    gate = _agent_write_gate("grant_access", {"subject": subject, "object": obj})
    if gate is not None:
        return gate
    if relation not in {"owner", "editor", "viewer"}:
        return _err("relation must be one of: owner, editor, viewer")
    try:
        from examlops.data.governance import grant_relation

        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "mcp-agent"
        grant_relation(subject, relation, obj, actor=actor)
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    return _with_audit(
        {"ok": True, "subject": subject, "relation": relation, "object": obj},
        "access_granted",
        obj,
        {"subject": subject, "relation": relation},
    )


@dataclass(frozen=True)
class ToolSpec:
    """Metadata describing one agent-callable tool."""

    fn: Callable[..., dict[str, Any]]
    mutating: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)
    use_cases: tuple[str, ...] = field(default_factory=tuple)
    tier: str = "read"

    @property
    def name(self) -> str:
        return self.fn.__name__

    @property
    def description(self) -> str:
        doc = (self.fn.__doc__ or "").strip()
        # First paragraph is the summary agents see in tool listings.
        return doc.split("\n\n", 1)[0].replace("\n", " ").strip() if doc else self.name


REGISTRY: tuple[ToolSpec, ...] = (
    # ── status / registry / management ────────────────────────────────────────
    ToolSpec(platform_status, tags=("read", "status"), use_cases=("monitoring", "incident")),
    ToolSpec(list_models, tags=("read", "registry"), use_cases=("management",)),
    ToolSpec(model_detail, tags=("read", "registry"), use_cases=("management",)),
    ToolSpec(
        list_production_models,
        tags=("read", "registry"),
        use_cases=("management", "monitoring"),
    ),
    ToolSpec(list_approvals, tags=("read", "governance"), use_cases=("governance", "management")),
    ToolSpec(modelzoo_status, tags=("read", "modelzoo"), use_cases=("management",)),
    ToolSpec(
        recent_audit_events,
        tags=("read", "governance", "audit"),
        use_cases=("governance", "incident"),
    ),
    ToolSpec(
        trigger_retrain,
        mutating=True,
        tags=("write", "training"),
        use_cases=("management",),
        tier="B",
    ),
    ToolSpec(retrain_status, tags=("read", "training"), use_cases=("management",)),
    # ── HPC / fleet ───────────────────────────────────────────────────────────
    ToolSpec(hpc_clusters, tags=("read", "hpc"), use_cases=("management",)),
    ToolSpec(hpc_nodes, tags=("read", "hpc", "monitoring"), use_cases=("monitoring",)),
    ToolSpec(hpc_place, tags=("read", "hpc"), use_cases=("management",)),
    ToolSpec(
        fleet_simulate, tags=("read", "hpc", "fleet", "finops"), use_cases=("finops", "management")
    ),
    ToolSpec(hpc_jobs, tags=("read", "hpc", "monitoring"), use_cases=("monitoring",)),
    ToolSpec(
        hpc_approve_cluster,
        mutating=True,
        tags=("write", "hpc", "governance"),
        use_cases=("governance",),
        tier="B",
    ),
    # ── projects & workspaces ─────────────────────────────────────────────────
    ToolSpec(project_list, tags=("read", "projects"), use_cases=("management",)),
    ToolSpec(project_detail, tags=("read", "projects"), use_cases=("management",)),
    ToolSpec(project_cost, tags=("read", "projects", "finops"), use_cases=("finops",)),
    ToolSpec(
        project_assign_model,
        mutating=True,
        tags=("write", "projects"),
        use_cases=("management",),
        tier="A",
    ),
    ToolSpec(
        project_add_member,
        mutating=True,
        tags=("write", "projects", "governance"),
        use_cases=("governance",),
        tier="B",
    ),
    # ── monitoring & drift ────────────────────────────────────────────────────
    ToolSpec(
        drift_status, tags=("read", "drift", "monitoring"), use_cases=("monitoring", "incident")
    ),
    ToolSpec(input_drift_status, tags=("read", "drift", "monitoring"), use_cases=("monitoring",)),
    ToolSpec(
        list_drift, tags=("read", "drift", "monitoring"), use_cases=("monitoring", "incident")
    ),
    ToolSpec(get_drift_autoretrain, tags=("read", "drift"), use_cases=("monitoring", "management")),
    # ── serving / traffic / promotion ─────────────────────────────────────────
    ToolSpec(traffic_rules, tags=("read", "serving"), use_cases=("management",)),
    ToolSpec(promotion_rule, tags=("read", "serving"), use_cases=("management",)),
    ToolSpec(
        challenger_config,
        tags=("read", "serving", "monitoring"),
        use_cases=("management", "monitoring"),
    ),
    ToolSpec(autoscale_config, tags=("read", "serving"), use_cases=("management",)),
    ToolSpec(scale_events, tags=("read", "serving", "monitoring"), use_cases=("monitoring",)),
    # ── SLO / fairness / governance ───────────────────────────────────────────
    ToolSpec(
        slo_specs, tags=("read", "governance", "quality"), use_cases=("monitoring", "governance")
    ),
    ToolSpec(
        slo_ratio, tags=("read", "governance", "quality"), use_cases=("monitoring", "governance")
    ),
    ToolSpec(fairness_status, tags=("read", "governance", "quality"), use_cases=("governance",)),
    ToolSpec(compliance_system, tags=("read", "governance"), use_cases=("governance",)),
    ToolSpec(authz_relations, tags=("read", "governance", "audit"), use_cases=("governance",)),
    # ── FinOps / Green-AI ─────────────────────────────────────────────────────
    ToolSpec(model_costs, tags=("read", "finops"), use_cases=("finops",)),
    ToolSpec(carbon, tags=("read", "finops"), use_cases=("finops",)),
    ToolSpec(platform_cost_summary, tags=("read", "finops"), use_cases=("finops",)),
    ToolSpec(gateway_cost, tags=("read", "finops", "gateway"), use_cases=("finops",)),
    # ── gateway / LLMOps ──────────────────────────────────────────────────────
    ToolSpec(gateway_config, tags=("read", "gateway", "llmops"), use_cases=("management",)),
    ToolSpec(
        gateway_cache_stats, tags=("read", "gateway", "finops"), use_cases=("finops", "monitoring")
    ),
    ToolSpec(list_gateway_keys, tags=("read", "gateway", "governance"), use_cases=("governance",)),
    # ── evaluation / data ─────────────────────────────────────────────────────
    ToolSpec(eval_gate, tags=("read", "eval", "quality"), use_cases=("monitoring", "governance")),
    ToolSpec(eval_results, tags=("read", "eval", "quality"), use_cases=("monitoring",)),
    ToolSpec(
        gate_reports, tags=("read", "eval", "quality"), use_cases=("governance", "monitoring")
    ),
    ToolSpec(dataset_revisions, tags=("read", "data"), use_cases=("management",)),
    ToolSpec(data_quality, tags=("read", "data", "quality"), use_cases=("monitoring",)),
    ToolSpec(feature_view, tags=("read", "data"), use_cases=("management",)),
    # ── dataplane (ADR 0130) ──────────────────────────────────────────────────
    ToolSpec(dataplane_sources, tags=("read", "data"), use_cases=("management",)),
    ToolSpec(dataplane_snapshots, tags=("read", "data"), use_cases=("management",)),
    ToolSpec(
        dataplane_pull,
        mutating=True,
        tags=("write", "data"),
        use_cases=("management",),
        tier="A",
    ),
    # ── incident / lineage ────────────────────────────────────────────────────
    ToolSpec(
        model_lineage, tags=("read", "lineage", "incident"), use_cases=("incident", "management")
    ),
    ToolSpec(lineage_impact, tags=("read", "lineage", "incident"), use_cases=("incident",)),
    # ── help / discoverability ────────────────────────────────────────────────
    ToolSpec(explain_command, tags=("read", "help", "docs"), use_cases=("help",)),
    # ── configuration writes (Phase 5, gated + tiered) ────────────────────────
    ToolSpec(
        set_traffic_split,
        mutating=True,
        tags=("write", "serving"),
        use_cases=("management",),
        tier="A",
    ),
    ToolSpec(
        disable_challenger,
        mutating=True,
        tags=("write", "serving"),
        use_cases=("management",),
        tier="A",
    ),
    ToolSpec(
        set_drift_autoretrain,
        mutating=True,
        tags=("write", "drift"),
        use_cases=("management",),
        tier="B",
    ),
    ToolSpec(
        set_promotion_rule,
        mutating=True,
        tags=("write", "serving"),
        use_cases=("management",),
        tier="B",
    ),
    ToolSpec(
        grant_access,
        mutating=True,
        tags=("write", "governance"),
        use_cases=("governance",),
        tier="C",
    ),
)


def capabilities_catalogue(include_writes: bool | None = None) -> dict[str, Any]:
    """Group the tool registry by lifecycle use case — the capabilities catalogue.

    Derived directly from :data:`REGISTRY`, so ``exa mcp capabilities``, the A2A Agent Card,
    and any capabilities digest injected into the agent's system prompt all share one source
    and can never drift. Tools with no ``use_cases`` are surfaced under ``other``.

    Returns a mapping ``{use_case: [{"name", "description", "mutating", "tier", "tags"}, …]}``
    ordered by :data:`USE_CASES`.
    """
    specs = list(iter_tools(include_writes=include_writes))
    catalogue: dict[str, list[dict[str, Any]]] = {uc: [] for uc in USE_CASES}
    catalogue["other"] = []
    for spec in specs:
        entry = {
            "name": spec.name,
            "description": spec.description,
            "mutating": spec.mutating,
            "tier": spec.tier,
            "tags": list(spec.tags),
        }
        targets = spec.use_cases or ("other",)
        for uc in targets:
            catalogue.setdefault(uc, []).append(entry)
    # Drop empty buckets for a clean catalogue.
    return {uc: tools for uc, tools in catalogue.items() if tools}


def iter_tools(include_writes: bool | None = None) -> Iterator[ToolSpec]:
    """Yield registered tools.

    Args:
        include_writes: Whether to include mutating tools. When ``None`` (default), the
            value is read from the ``EXAMLOPS_MCP_ALLOW_WRITES`` environment variable.
    """
    if include_writes is None:
        include_writes = _writes_enabled()
    off = _disabled_modules()
    for spec in REGISTRY:
        if spec.mutating and not include_writes:
            continue
        # Site feature profile (ADR 0128): a tool of a module this site switched off is not
        # offered to agents at all — the same decision the CLI and the dashboard enforce.
        if off and set(_modules_for_tags(spec.tags)) & off:
            continue
        yield spec


def _modules_for_tags(tags: tuple[str, ...]) -> list[str]:
    from examlops.lifecycle.modules import modules_for_tags

    return modules_for_tags(tags)


def _disabled_modules() -> set[str]:
    """Modules the site profile switches off; empty (nothing filtered) if it cannot be read."""
    try:
        from examlops.lifecycle.modules import resolve

        return set(resolve().disabled_ids())
    except Exception:  # noqa: BLE001 — a broken profile must not take the agent surface down
        return set()


def _writes_enabled() -> bool:
    return os.getenv("EXAMLOPS_MCP_ALLOW_WRITES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
