"""MCP prompts for ExaMLOps — reusable, expert-authored workflows for agents.

Prompts encode the *right way* to perform common operational tasks (diagnose drift, promote
a model safely, triage the platform) so any agent — or a human via an MCP client — gets a
consistent, safe procedure instead of improvising. Each prompt returns guidance text that
references the ExaMLOps tools/resources the agent already has.

Pure and FastMCP-free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


def diagnose_drift(model: str) -> str:
    """Guide the agent through diagnosing prediction/input drift for a model."""
    return (
        f"You are diagnosing drift for the model '{model}' on the ExaMLOps platform.\n\n"
        "Follow this procedure:\n"
        f"1. Call `platform_status` to confirm services are healthy.\n"
        f"2. Read the resource `examlops://model/{model}` to see current aliases/versions.\n"
        f"3. Review `examlops://audit/recent` for recent retrains or promotions of '{model}'.\n"
        "4. Summarise: is drift prediction-level or input-level? Is a retrain already in flight?\n"
        "5. Recommend a concrete next action (baseline reset, retrain, or no action) with "
        "the exact `exa` command. Do NOT trigger a retrain yourself unless explicitly asked."
    )


def promote_safely(model: str) -> str:
    """Guide the agent through a safe, metric-gated model promotion."""
    return (
        f"You are helping promote the model '{model}' to Production safely.\n\n"
        "Follow this checklist before recommending promotion:\n"
        f"1. Read `examlops://model/{model}` — compare the Staging vs Production versions.\n"
        "2. Confirm the candidate passed validation (latency SLA) and metric gates.\n"
        "3. Check `examlops://audit/recent` for the candidate's training lineage.\n"
        "4. Verify no pending approvals block the change.\n"
        "5. Recommend the exact gated command, e.g. "
        f"`exa pipeline promote {model.lower()} --if-rmse-lt <threshold>`, and note that a "
        "human/approval gate applies. Never bypass the approval gate."
    )


def platform_triage() -> str:
    """Guide the agent through triaging overall platform health."""
    return (
        "Triage the ExaMLOps platform's current health.\n\n"
        "1. Call `platform_status` and list any unreachable services.\n"
        "2. Read `examlops://models` and flag models with no Production alias.\n"
        "3. Check `list_approvals` for anything blocking releases.\n"
        "4. Scan `examlops://audit/recent` for failures or unusual activity.\n"
        "5. Produce a short prioritised report: what is broken, what needs attention, and the "
        "single most important `exa` command to run next."
    )


def review_project(name: str) -> str:
    """Guide the agent through auditing a Project workspace (ADR 0086)."""
    return (
        f"You are reviewing the ExaMLOps Project (workspace) '{name}'.\n\n"
        "Follow this procedure:\n"
        f"1. Read the resource `examlops://project/{name}` for the full anatomy — resources by "
        "kind, members and their roles, quota, budget, and consumption.\n"
        f"2. Call `project_cost` for '{name}' to see attributed GPU-hours, USD, carbon, and "
        "whether it is over budget or GPU quota.\n"
        "3. Flag governance risks: is there at least one owner? Any editor/viewer that looks "
        "stale? Resources assigned but no members, or vice-versa?\n"
        "4. Flag FinOps risks: consumption near or over budget; models with cost but not "
        "attributed to any project (unaccounted spend).\n"
        "5. Produce a short prioritised report. If a change is warranted (add an owner, adjust "
        "the budget, assign an orphaned model), recommend the exact `exa project ...` command. "
        "Do NOT make membership or assignment changes yourself unless explicitly asked — those "
        "are gated, audited writes."
    )


@dataclass(frozen=True)
class PromptSpec:
    fn: Callable[..., str]
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return self.fn.__name__

    @property
    def description(self) -> str:
        doc = (self.fn.__doc__ or "").strip()
        return doc.split("\n\n", 1)[0].strip() if doc else self.name


PROMPTS: tuple[PromptSpec, ...] = (
    PromptSpec(diagnose_drift, tags=("drift", "diagnostics")),
    PromptSpec(promote_safely, tags=("promotion", "governance")),
    PromptSpec(platform_triage, tags=("status", "diagnostics")),
    PromptSpec(review_project, tags=("projects", "governance", "finops")),
)


def iter_prompts() -> tuple[PromptSpec, ...]:
    return PROMPTS
