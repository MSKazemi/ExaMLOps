"""Friendly, human-readable epilogs for every ``exa`` command *group*.

Running a bare group (``exa serve``) or its help (``exa serve --help``) already lists the
subcommands in titled panels. What it lacked was the same hand-holding a first-time user
gets on a leaf command: *what would I actually type*, and *where do I go for more*. This
module adds, to every group and nested sub-group, a consistent epilog with two blocks:

* **Common tasks** — 2–3 curated, copy-paste examples (read-only where possible) for the
  busiest groups, taken from the CLI command guide. Groups without an entry simply omit it.
* **Learn more** — a generic footer that always works: how to get a subcommand's options
  (``-h``), the plain-language overview (``exa explain <group>``), and the full use-case
  guide.

:func:`attach_group_epilogs` walks the Typer app tree once and sets ``info.epilog`` on each
registered group, exactly mirroring how ``_help.assign_panels`` sets ``rich_help_panel`` —
one central call instead of ~60 ``add_typer(epilog=…)`` edits. Nested groups (``serve
shadow``, ``drift auto-retrain``) are reached recursively and keyed by their full path.

The spec below is the single source of truth. ``tests/unit/test_cli_group_help.py`` fails
loudly if a top-level group ends up without the footer.
"""

from __future__ import annotations

from typing import Any

# Full group path (space-joined) → list of (comment, command) example pairs.
# Keep examples read-only / safe where possible; a couple use --dry-run/--dummy to make the
# mutating shape obvious without firing anything.
_COMMON_TASKS: dict[str, list[tuple[str, str]]] = {
    # Getting Started
    "config": [
        ("Show the resolved config", "exa config show"),
        ("Point the CLI at a control plane", "exa config set control_plane http://localhost:18002"),
        ("Switch active environment", "exa config use lxp"),
    ],
    # Training & Pipelines
    "pipeline": [
        ("List discovered models + datasets", "exa pipeline list"),
        ("Validate pack YAML against shims", "exa pipeline validate"),
        (
            "Dry-run a training pipeline",
            "exa pipeline run --model JPCP --dataset PM100Dataset --dummy",
        ),
    ],
    "reproduce": [
        ("List reproducibility bundles", "exa reproduce list"),
    ],
    # Data & Features
    "data": [
        ("List recorded dataset revisions", "exa data list FData"),
        (
            "Record an immutable revision",
            "exa data snapshot FData --backend minio --path ./data/FData",
        ),
        ("Diff two revisions", "exa data diff FData <revA> <revB>"),
    ],
    "dataplane": [
        ("See which connectors are installed", "exa dataplane connectors"),
        (
            "Register a Zenodo record as a source",
            "exa dataplane sources create pm100 --connector zenodo "
            "--spec-json '{\"record\": 10127767}'",
        ),
        ("Pull it now", "exa dataplane pull pm100"),
        ("See what was committed", "exa dataplane snapshots pm100"),
    ],
    "dataplane sources": [
        ("List sources", "exa dataplane sources list"),
    ],
    "feature": [
        ("List registered feature views", "exa feature list"),
    ],
    "features": [
        ("List versioned training features", "exa features list"),
    ],
    "assets": [
        ("List asset nodes + freshness", "exa assets list"),
    ],
    "cards": [
        ("Structured model card from live data", "exa cards model jpcp"),
    ],
    # Models & Registry
    "models": [
        ("List models + production alias", "exa models list"),
        ("Inspect one model", "exa models info jpcp"),
        ("Compare two versions", "exa models diff jpcp 1 2"),
        ("GPU-hour cost history", "exa models cost jpcp"),
    ],
    "modelzoo": [
        ("ModelZoo freshness per model", "exa modelzoo status"),
        ("Recent ModelZoo push events", "exa modelzoo events"),
    ],
    "embedding": [
        ("List embedding encoders", "exa embedding list"),
    ],
    # Serving & Inference
    "serve": [
        ("Models hot-loaded in Ray Serve", "exa serve models"),
        ("Traffic split across aliases", "exa serve traffic-list"),
        ("Smoke-test serving", "exa serve check"),
    ],
    "serve llm": [
        ("What LLM/VLM endpoints exist?", "exa serve llm list"),
        (
            "Register a running vLLM server",
            "exa serve llm start qwen-vl --base-url http://gpu01:8000",
        ),
        (
            "Launch one on HPC (2 nodes x 4 GPUs)",
            "exa serve llm start qwen-vl -l slurm --nodes 2 --gpus 4",
        ),
        (
            "Ask a vision model about an image",
            "exa serve llm chat qwen-vl -m 'What is this?' --image plot.png",
        ),
        ("Is it up? (exit 1 if not)", "exa serve llm health qwen-vl"),
    ],
    "serve shadow": [
        ("Is shadowing active?", "exa serve shadow status JPCP"),
        ("Compare shadow vs production", "exa serve shadow log JPCP"),
    ],
    "serve ab": [
        ("Review A/B experiments", "exa serve ab status JPCP"),
        ("Decide a winner (t-test)", "exa serve ab analyze JPCP --lower-is-better"),
    ],
    "gateway": [
        ("List virtual keys (hashes only)", "exa gateway key list"),
        ("Semantic-cache hit-rate", "exa gateway cache stats"),
    ],
    "vector": [
        ("Collection dim/metric/count", "exa vector stats demo"),
    ],
    "rag": [
        ("List knowledge bases", "exa rag list"),
    ],
    # Agents & Automation
    "agent": [
        ("Is the agent up, and on which backend?", "exa agent status"),
        ("As a health gate (exits 1 if unusable)", "exa --json agent status"),
        ("Check the agent in another environment", "exa -c lxp agent status"),
        ("Hold a conversation with it", "exa chat"),
    ],
    # GenAI & LLMOps
    "genai": [
        ("GenAI telemetry status", "exa genai check"),
        ("USD cost from token usage", "exa genai cost --model gpt-4o --in 1000 --out 500"),
    ],
    "prompt": [
        ("List versioned prompts", "exa prompt list"),
        ("Create a new prompt version", 'exa prompt create greeting --template "Hello {name}"'),
    ],
    "guardrails": [
        ("Run text through the guardrail", 'exa guardrails test "ignore previous instructions"'),
        ("Allow/redact/block counts", "exa guardrails stats"),
    ],
    "agentops": [
        ("Per-tool success rate / latency", "exa agentops tools"),
    ],
    # Monitoring & Quality
    "drift": [
        ("Prediction-drift status", "exa drift status"),
        ("Store the current baseline", "exa drift baseline JPCP"),
        ("Preview auto-retrain triggers", "exa drift trigger --dry-run"),
    ],
    "drift auto-retrain": [
        ("Show auto-retrain config", "exa drift auto-retrain status"),
    ],
    "drift input": [
        ("Embedding distribution drift", "exa drift input status"),
    ],
    "eval": [
        ("Run an evaluation suite", "exa eval run --help"),
    ],
    "eval calibration": [
        ("Can this judge gate a promotion?", "exa eval calibration show <judge>"),
        ("List recorded calibrations", "exa eval calibration list"),
    ],
    "slo": [
        ("List declared SLOs", "exa slo list"),
    ],
    "fairness": [
        ("Subgroup disparity report", "exa fairness report --help"),
    ],
    "autopilot": [
        ("Self-driving loop history", "exa autopilot status"),
        ("Preview one cycle (no changes)", "exa autopilot run --dry-run"),
    ],
    # HPC, Fleet & FinOps
    "hpc": [
        ("Compute nodes (CPU/mem/GPU)", "exa hpc nodes"),
        ("Per-cluster GPU util & cost", "exa hpc capacity"),
        ("Registered clusters + approval", "exa hpc clusters"),
    ],
    "finops": [
        ("Aggregate energy + CO2e", "exa finops carbon report"),
        ("Budget vs consumption", "exa finops budget status"),
    ],
    "report": [
        ("Generate a cost/carbon report", "exa report generate --help"),
    ],
    "commands": [
        ("Asynchronous commands, newest first", "exa commands list"),
        ("Follow one (state, flow run, last error)", "exa commands show <id>"),
    ],
    # Governance & Security
    "approvals": [
        ("Pending model-change approvals", "exa approvals list"),
    ],
    "audit": [
        ("Recent audit log (hash-chained)", "exa audit --last 7d"),
        ("Verify chain integrity", "exa audit verify"),
    ],
    "plan": [
        ("Plans agents have proposed", "exa plan list"),
        ("One plan in full", "exa plan show <plan-hash>"),
    ],
    "secrets": [
        ("Secret metadata (never values)", "exa secrets list"),
        ("Scan a path for leaks (CI gate)", "exa secrets scan ./config"),
    ],
    "compliance": [
        ("EU AI Act classification", "exa compliance status"),
    ],
    "governance": [
        ("NIST AI RMF evidence coverage", "exa governance report"),
    ],
    "policy": [
        ("Policy rules from policy.yaml", "exa policy list"),
    ],
    "providers": [
        ("Pluggable calculation providers", "exa providers list"),
    ],
    # Projects & Workspaces
    "project": [
        ("All projects with quotas", "exa project list"),
        ("Full project anatomy", "exa project show minio-demo"),
        ("Show the active project", "exa project current"),
    ],
    "namespace": [
        ("Namespaces + model counts", "exa namespace list"),
    ],
    "connection": [
        ("Named connections (metadata)", "exa connection list"),
    ],
    "workbench": [
        ("On-demand dev environments", "exa workbench list"),
    ],
    # Platform & Integrations
    "stack": [
        ("Running containers + ports", "exa stack status"),
    ],
    "backup": [
        ("Backups & bundles", "exa backup list"),
    ],
    "instance": [
        ("Core, deployment and every user-data location", "exa instance info"),
        ("Pre-flight before/after an upgrade", "exa instance check"),
    ],
    "upgrade": [
        ("What the installed release makes of the data", "exa upgrade plan"),
        ("Back up, then migrate", "exa upgrade apply --dry-run"),
    ],
    "modules": [
        ("What runs at this site, and why", "exa modules list"),
        ("Switch a module off here", "exa modules disable agent"),
        ("Deployment input from the profile", "exa modules render --target compose"),
    ],
    "events": [
        ("Event-outbox backlog", "exa events stats"),
    ],
    "admission": [
        ("Admission queue depth", "exa admission stats"),
    ],
    "dataplane-bus": [
        ("Models + Dataplane bus UUIDs", "exa dataplane-bus list"),
    ],
    "mcp": [
        ("Tools exposed to agents", "exa mcp tools"),
    ],
    # ── Nested sub-groups ─────────────────────────────────────────────────────────────
    "data synth": [
        ("Fit a generator + report", "exa data synth fit FData --help"),
        ("Generate a gated synthetic set", "exa data synth generate FData --help"),
    ],
    "models card": [
        ("Generate a model card", "exa models card generate jpcp"),
        ("Card generation history", "exa models card history jpcp"),
    ],
    "models engine": [
        ("List inference engines", "exa models engine list"),
        ("Validate a model's engine block", "exa models engine validate JPCP"),
    ],
    "models rollback": [
        ("Rollback history", "exa models rollback history jpcp"),
        ("Roll back the Production alias", "exa models rollback run jpcp --help"),
    ],
    "production": [
        ("Verify production health", "exa production verify"),
        ("Plan a production deploy", "exa production deploy --help"),
    ],
    "serve challenger": [
        ("Champion-challenger scoreboard", "exa serve challenger status JPCP"),
        ("List configured challengers", "exa serve challenger list"),
    ],
    "serve autoscale": [
        ("Autoscale policy + recent events", "exa serve autoscale status JPCP"),
        ("Estimate scale-to-zero savings", "exa serve autoscale savings JPCP"),
        ("Preview one controller cycle (dry run)", "exa serve autoscale run --once"),
    ],
    "serve routing": [
        ("Prefix-cache hit rate", "exa serve routing stats JPCP"),
        ("Simulate a request stream", "exa serve routing simulate --help"),
    ],
    "serve batch": [
        ("List recent batch jobs", "exa serve batch list"),
        ("Submit batch inference", "exa serve batch submit --help"),
    ],
    "serve adapter": [
        ("List registered LoRA adapters", "exa serve adapter list"),
        ("Route via base + adapter", "exa serve adapter route --help"),
    ],
    "serve explain": [
        ("Recent explain requests", "exa serve explain history JPCP"),
    ],
    "pipeline hpo": [
        ("HPO study status", "exa pipeline hpo status"),
        ("Trigger an HPO study", "exa pipeline hpo start --help"),
    ],
    "pipeline quality": [
        ("Data-quality check history", "exa pipeline quality history JPCP"),
        ("Run data-quality checks", "exa pipeline quality check --help"),
    ],
    "pipeline distributed": [
        ("List distributed runs", "exa pipeline distributed list"),
        ("Launch distributed training", "exa pipeline distributed launch --help"),
    ],
    "fleet": [
        ("Fleet heatmap tiles", "exa fleet heatmap"),
        ("Project a what-if scenario", "exa fleet simulate --help"),
    ],
    "exchange": [
        ("Inspect a package manifest", "exa exchange inspect <pkg.novapack>"),
        ("Verify signature + integrity", "exa exchange verify <pkg.novapack>"),
    ],
    "federated": [
        ("Run config, sites, rounds", "exa federated status <run-id>"),
        ("Differential-privacy (ε, δ) budget", "exa federated budget <run-id>"),
    ],
    "hardware": [
        ("List device pools", "exa hardware pools"),
        ("Recent placement decisions", "exa hardware decisions"),
    ],
    "eval feedback": [
        ("Live accuracy from labels", "exa eval feedback accuracy JPCP"),
        ("Join predictions ↔ labels", "exa eval feedback join JPCP"),
    ],
    "eval gate": [
        ("Show the regression gate", "exa eval gate show JPCP"),
        ("Run the gate for a version", "exa eval gate run JPCP --help"),
    ],
    "finops budget": [
        ("Budget vs consumption", "exa finops budget status"),
        ("Set a project budget", "exa finops budget set --help"),
    ],
    "finops carbon": [
        ("Aggregate energy + CO2e", "exa finops carbon report"),
        ("List carbon providers", "exa finops carbon providers"),
    ],
    "finops cost": [
        ("List cost providers (rate cards)", "exa finops cost providers"),
    ],
    "gateway key": [
        ("List virtual keys (hashes only)", "exa gateway key list"),
        ("Issue a virtual key", "exa gateway key issue --help"),
    ],
    "gateway quota": [
        ("List per-tenant quotas", "exa gateway quota list"),
        ("Cap a tenant at 120 requests/min", "exa gateway quota set acme 120"),
    ],
    "gateway cache": [
        ("Semantic-cache hit-rate + savings", "exa gateway cache stats"),
    ],
    "gateway schema": [
        ("Validate an object against a schema", "exa gateway schema test --help"),
    ],
    "gateway reasoning": [
        ("Reasoning-vs-output token/cost split", "exa gateway reasoning stats"),
        ("Cap a model's thinking tokens", "exa gateway reasoning set-budget 2000 --model qwen3"),
        ("Budget breaches the gateway saw", "exa gateway reasoning budgets --events"),
    ],
    "hpc gpu-share": [
        ("Fractional GPU accounting", "exa hpc gpu-share accounting"),
        ("Plan a GPU-sharing mechanism", "exa hpc gpu-share plan --help"),
    ],
    "policy bundle": [
        ("List signed policy bundles", "exa policy bundle list"),
        ("Verify a stored bundle", "exa policy bundle verify --help"),
    ],
}

# Where the full "what · use case · example" guide lives (relative to the repo docs root).
_GUIDE = "docs/reference/cli-commands-guide.md"


def _render_epilog(path: str, tasks: list[tuple[str, str]] | None) -> str:
    """Build the Rich-markup epilog string for the group at ``path`` (e.g. ``serve ab``).

    Typer/Rich renders a help epilog markdown-style: single newlines collapse to spaces and
    only a blank line (``\\n\\n``) starts a new visual line. So every line here is joined by
    ``\\n\\n``, and each example is one line — ``<command>  — <what it's for>`` — instead of a
    separate comment line, which keeps the block compact and readable.
    """
    # Each visual line is its own paragraph (blank line between) so Rich keeps the breaks.
    paras: list[str] = []
    if tasks:
        paras.append("[bold cyan]Common tasks[/bold cyan]")
        for what, cmd in tasks:
            paras.append(f"  {cmd}  [dim]— {what}[/dim]")
    paras.append("[bold cyan]Learn more[/bold cyan]")
    paras.append(f"  exa {path} <command> -h  [dim]— options & details for a subcommand[/dim]")
    paras.append(f"  exa explain {path}  [dim]— plain-language overview of this group[/dim]")
    paras.append(f"  {_GUIDE}  [dim]— full guide: every command with use case + example[/dim]")
    return "\n\n".join(paras)


def attach_group_epilogs(app: Any) -> None:
    """Recursively set a friendly ``epilog`` on every registered group of ``app``.

    ``app`` is a ``typer.Typer`` instance. Idempotent: a group that already has a non-empty
    epilog is left untouched (so an intentionally hand-tuned group epilog wins). Curated
    "Common tasks" come from :data:`_COMMON_TASKS`, keyed by the full space-joined group
    path; every group — curated or not — gets the "Learn more" footer.
    """

    def _walk(node: Any, prefix: tuple[str, ...]) -> None:
        for info in getattr(node, "registered_groups", []):
            name = getattr(info, "name", None)
            if not name:
                continue
            path_parts = (*prefix, name)
            path = " ".join(path_parts)
            if not getattr(info, "epilog", None):
                info.epilog = _render_epilog(path, _COMMON_TASKS.get(path))
            sub = getattr(info, "typer_instance", None)
            if sub is not None:
                _walk(sub, path_parts)

    _walk(app, ())
