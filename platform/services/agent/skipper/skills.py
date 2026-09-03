"""Specialist skill packs for the supervisor topology (Phase 4, ADR 0100).

Each :class:`Specialist` is a scoped view of the platform's capabilities — a subset of tools plus
a focused playbook and trigger keywords the deterministic router matches against. The supervisor
(``skipper/supervisor.py``) compiles one ``create_agent`` per specialist so a small local
model only ever sees ~10–20 relevant tools per turn instead of the full ~50 — the single biggest
lever on tool-selection accuracy.

A pack draws from **two sources on purpose**:

* **Broad reads** from ``examlops.mcp.tools`` (the single source of truth, which carries the
  ``use_cases`` metadata and, since Phase 1, covers drift/serving/SLO/fairness/FinOps/gateway/eval),
  wrapped as LangChain tools via the existing ``mcp_bridge`` wrapper.
* **Gated writes + agent-specific ops** from the in-repo tool set (``skipper.tools``) — these already
  carry the ``@confirmed_write`` HITL interrupt, so keeping them avoids re-implementing write safety.

Store-backed memory tools + the agent-side ``search_knowledge`` are appended to every pack so memory
and grounded help are always reachable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Specialist:
    """One routing target: a scoped toolset + playbook + trigger keywords."""

    name: str
    use_cases: tuple[str, ...]  # MCP read tools whose use_cases intersect are included
    inrepo: tuple[str, ...]  # in-repo tool names (reads/ops/gated-writes) to include
    triggers: tuple[str, ...]
    playbook: str


# In-repo tools every specialist benefits from (grounded help + a quick health read).
_CORE_INREPO = ("search_knowledge", "platform_health")

SPECIALISTS: tuple[Specialist, ...] = (
    Specialist(
        name="manager",
        use_cases=("management",),
        inrepo=(
            "list_models",
            "describe_model",
            "list_datasets",
            "list_pipeline_models",
            "trigger_retrain",
            "get_retrain_status",
            "reload_models",
            "list_loaded_models",
            "predict",
            "predict_pipeline",
            "set_traffic_split",
            "promote_model",
            "trigger_auto_retrain",
            "validate_model_serving",
            "compare_model_versions",
            "get_model_lineage",
            "list_deployments",
            "list_runs",
            "scaffold_preview",
            "scaffold_create",
            "modelzoo_sync",
            "modelzoo_set_config",
            "restart_service",
            "start_service",
            "stop_service",
        ),
        triggers=(
            "retrain",
            "promote",
            "deploy",
            "rollback",
            "traffic",
            "canary",
            "scaffold",
            "assign",
            "register",
            "reload",
            "serve",
            "restart",
            "start service",
            "stop service",
        ),
        playbook=(
            "You are the MANAGEMENT specialist. You drive lifecycle operations — retrain, promote, "
            "traffic splits, serving, projects, HPC placement. Prefer is_dummy for retrains unless "
            "told otherwise. Every mutating action is confirmation-gated: describe exactly what "
            "will change before acting and wait for approval."
        ),
    ),
    Specialist(
        name="monitor",
        use_cases=("monitoring", "incident"),
        inrepo=(
            "platform_health",
            "get_metrics",
            "generate_report",
            "get_drift_status",
            "get_input_drift_status",
            "diagnose_platform",
            "get_platform_summary",
            "query_audit_log",
            "list_loaded_models",
            "validate_model_serving",
            "list_services",
            "service_logs",
            "recall_baseline",
        ),
        triggers=(
            "status",
            "health",
            "drift",
            "how are things",
            "any issues",
            "anomaly",
            "anomalies",
            "slo",
            "latency",
            "incident",
            "diagnose",
            "what's wrong",
            "alert",
            "scale",
            "logs",
        ),
        playbook=(
            "You are the MONITORING/SRE specialist. Assess platform health by cross-referencing "
            "multiple signals — drift, SLO ratios, health, recent audit events — never a single "
            "metric. Lead with the top concerns ordered by severity and the specific tool/command "
            "that addresses each. You are read-only; recommend gated actions, do not perform them."
        ),
    ),
    Specialist(
        name="helper",
        use_cases=("help",),
        inrepo=("search_knowledge", "search_docs", "read_doc", "list_docs", "get_howto"),
        triggers=("how do i", "how to", "what is", "explain", "guide", "docs", "tutorial", "usage"),
        playbook=(
            "You are the HELP specialist. Answer 'how does it work / how do I …?' questions from "
            "the documentation. ALWAYS call search_knowledge first and ground your answer in the "
            "retrieved passages, citing the source paths. Use explain_command for exact CLI syntax."
        ),
    ),
    Specialist(
        name="finops",
        use_cases=("finops",),
        inrepo=(
            "get_metrics",
            "get_platform_summary",
            "get_cost_summary",
            "get_model_cost_history",
            "get_carbon_summary",
            "get_budget_status",
        ),
        triggers=("cost", "spend", "budget", "carbon", "green", "gpu-hour", "gpu hours", "finops"),
        playbook=(
            "You are the FinOps / Green-AI specialist. Report GPU-hour and USD cost, carbon "
            "(kgCO2e), gateway spend and project budgets. Flag budget/quota breaches and the "
            "biggest cost contributors. You are read-only."
        ),
    ),
    Specialist(
        name="governor",
        use_cases=("governance",),
        inrepo=(
            "list_pending_approvals",
            "approve_model",
            "reject_model",
            "query_audit_log",
        ),
        triggers=(
            "audit",
            "approval",
            "approve",
            "reject",
            "compliance",
            "policy",
            "fairness",
            "governance",
            "who has access",
            "permission",
            "relation",
        ),
        playbook=(
            "You are the GOVERNANCE / compliance specialist. Surface pending approvals, audit "
            "trail, compliance classification, fairness gates and access relations. High-privilege "
            "writes (grant access, revoke keys) are human-CLI-only — never perform them; explain "
            "the exact command a human should run."
        ),
    ),
)

# Low-confidence fallback: a read-only generalist over every use case + the safe in-repo reads.
GENERAL = Specialist(
    name="general",
    use_cases=("management", "monitoring", "help", "incident", "finops", "governance"),
    inrepo=(
        "platform_health",
        "diagnose_platform",
        "get_platform_summary",
        "search_knowledge",
        "list_models",
        "get_drift_status",
    ),
    triggers=(),
    playbook=(
        "You are the ExaMLOps platform assistant. Identify what the request needs, call tools "
        "proactively, cross-reference sources, and give clear, actionable answers. "
        # This generalist holds search_knowledge but used to be given no reason to reach for it, so
        # a question that reached it by falling through the router was answered from the model's
        # priors — confidently, and sometimes contrary to a rule the platform actually enforces.
        "The documentation is AUTHORITATIVE: before answering anything about what the platform "
        "supports, how it works, or whether something is allowed, call search_knowledge and ground "
        "the answer in what it returns, citing the source paths. Your own knowledge of MLOps is not "
        "a substitute — ExaMLOps has rules a general practitioner would not guess. If the search "
        "returns nothing, say the docs do not cover it rather than answering from memory."
    ),
)

ALL: tuple[Specialist, ...] = (*SPECIALISTS, GENERAL)


def by_name(name: str) -> Specialist:
    for s in ALL:
        if s.name == name:
            return s
    return GENERAL


def _mcp_reads_by_use_case(include_writes: bool | None):
    """Return ``{tool_name: wrapped_tool}`` for MCP reads, or ``{}`` if MCP is unavailable."""
    try:
        from examlops.mcp.tools import iter_tools
        from skipper.tools.mcp_bridge import _wrap
    except Exception:  # noqa: BLE001
        return {}, {}
    wrapped: dict = {}
    use_cases: dict = {}
    for spec in iter_tools(include_writes=include_writes):
        wrapped[spec.name] = _wrap(spec)
        use_cases[spec.name] = set(spec.use_cases)
    return wrapped, use_cases


def toolsets(
    inrepo_tools: list,
    *,
    include_writes: bool | None = None,
    extra_tools: list | None = None,
) -> dict[str, list]:
    """Build ``{specialist_name: [LangChain tools]}`` — MCP reads (by use_case) + in-repo tools.

    ``inrepo_tools`` is the in-repo ``skipper.tools.TOOLS`` list (LangChain tools). ``extra_tools``
    (store-backed memory tools) is appended to every pack. Returns ``{}`` only if there are no
    tools at all to scope (the caller then falls back to a single unscoped agent).
    """
    inrepo_by_name = {getattr(t, "name", getattr(t, "__name__", "")): t for t in inrepo_tools}
    mcp_wrapped, mcp_use_cases = _mcp_reads_by_use_case(include_writes)
    extra = list(extra_tools or [])

    packs: dict[str, list] = {}
    for spec in ALL:
        seen: set[str] = set()
        tools: list = []

        def _add(name: str, tool) -> None:
            if name and name not in seen:
                tools.append(tool)
                seen.add(name)

        # broad MCP reads whose use_cases intersect this specialist
        wanted = set(spec.use_cases)
        for name, uc in mcp_use_cases.items():
            if wanted & uc:
                _add(name, mcp_wrapped[name])
        # curated in-repo tools (gated writes + agent-specific ops)
        for name in (*spec.inrepo, *_CORE_INREPO):
            if name in inrepo_by_name:
                _add(name, inrepo_by_name[name])
        # memory / other cross-cutting extras on every pack
        for t in extra:
            _add(getattr(t, "name", getattr(t, "__name__", "")), t)

        packs[spec.name] = tools
    return packs
