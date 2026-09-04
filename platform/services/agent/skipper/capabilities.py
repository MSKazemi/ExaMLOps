"""Explicit capability metadata for agent-local tools.

Tools are unavailable to restricted graphs until they are classified here.  This is
deliberately fail-closed: adding a callable to ``skipper.tools.TOOLS`` does not make it
available to the dashboard Copilot by omission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ToolCapability:
    """Authority required to expose an agent-local tool."""

    name: str
    mutating: bool
    trust_tier: Literal["read", "A", "B", "C"]
    scopes: tuple[str, ...]


_READ_TOOLS = (
    "list_models",
    "describe_model",
    "list_datasets",
    "predict",
    "predict_pipeline",
    "list_loaded_models",
    "get_metrics",
    "platform_health",
    "generate_report",
    "list_pipeline_models",
    "get_retrain_status",
    "list_pending_approvals",
    "modelzoo_status",
    "modelzoo_events",
    "modelzoo_get_config",
    "list_services",
    "service_logs",
    "list_deployments",
    "list_runs",
    "scaffold_preview",
    "list_docs",
    "search_docs",
    "read_doc",
    "get_howto",
    "search_knowledge",
    "recall_baseline",
    "compare_model_versions",
    "get_model_lineage",
    "get_drift_status",
    "get_input_drift_status",
    "query_audit_log",
    "validate_model_serving",
    "get_platform_summary",
    "diagnose_platform",
    # FinOps reads (2026-09-04): cost/carbon/budget queries against platform.db — no mutation.
    "get_cost_summary",
    "get_model_cost_history",
    "get_carbon_summary",
    "get_budget_status",
)

_WRITE_TIERS: dict[str, Literal["A", "B", "C"]] = {
    "reload_models": "A",
    "trigger_retrain": "B",
    "approve_model": "B",
    "reject_model": "B",
    "modelzoo_sync": "A",
    "modelzoo_set_config": "B",
    "restart_service": "B",
    "start_service": "B",
    "stop_service": "B",
    "scaffold_create": "B",
    "set_traffic_split": "A",
    "promote_model": "B",
    "trigger_auto_retrain": "B",
}

CAPABILITIES: dict[str, ToolCapability] = {
    name: ToolCapability(name, False, "read", ("platform:read",)) for name in _READ_TOOLS
}
CAPABILITIES.update(
    {
        name: ToolCapability(name, True, tier, ("platform:write",))
        for name, tier in _WRITE_TIERS.items()
    }
)


def tool_name(tool: Any) -> str:
    """Return the stable public name of a LangChain or plain Python tool."""

    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def read_only_local_tools(tools: list[Any]) -> list[Any]:
    """Return only explicitly classified, non-mutating local tools."""

    return [
        tool
        for tool in tools
        if (capability := CAPABILITIES.get(tool_name(tool))) is not None and not capability.mutating
    ]


def unclassified_tool_names(tools: list[Any]) -> set[str]:
    """Identify tools that need an explicit capability decision."""

    return {name for tool in tools if (name := tool_name(tool)) not in CAPABILITIES}
