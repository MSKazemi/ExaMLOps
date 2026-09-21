"""The things `exa` manages, as resources — list · create · view · edit · delete · actions.

The CLI Console (ADR 0119) makes every command *runnable*. Operators, though, think in objects —
"the projects", "this workbench", "that virtual key" — and manage them the way every enterprise
console does: a table, a row, a menu of what can be done to it. This module declares, once, how
the CLI's verbs compose into those objects, so the dashboard's Resource Manager can render every
resource the same way without a page per resource:

* ``list`` is the command that enumerates the resource (its own options become the table's
  filters; a required one must be filled before anything is listed);
* ``key`` names the field in a listed row that identifies the item;
* ``create`` is a collection action; ``show`` / ``edit`` / ``delete`` / ``actions`` are row
  actions, whose parameters are pre-filled from the row. ``delete`` is reserved for commands that
  remove the item; stopping or disabling one (``serve ab stop``) is an action, not a deletion;
* ``bind`` states, per command, which parameter receives which row field — only where the
  default is wrong. The default binds the key to the parameter *named like the key*, else to the
  command's first positional argument; every other parameter whose name matches a row field is
  pre-filled too (e.g. a connection's ``project``).

Nothing here executes anything, and nothing here grants anything: each action is still one `exa`
command, run through the same tiers, argument validation, containment and audit as the console.
``tests/unit/test_cli_resources.py`` checks every command and binding against the live CLI tree,
and that every command group with a ``list`` verb is either a resource or explicitly not one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from examlops.cli import surface


@dataclass(frozen=True)
class Resource:
    id: str
    title: str
    description: str
    list: str
    key: str
    create: str | None = None
    show: str | None = None
    edit: tuple[str, ...] = ()
    delete: str | None = None
    actions: tuple[str, ...] = ()
    # command → {param: row field}, only where the default binding is wrong or missing.
    bind: dict[str, dict[str, str]] = field(default_factory=dict)
    # When the list command wraps its rows in an object with more than one array.
    rows_key: str | None = None
    # Preferred leading columns (the rest follow in the order the command emits them).
    columns: tuple[str, ...] = ()

    def row_commands(self) -> tuple[str, ...]:
        """Every command that acts on one listed item."""
        out = [c for c in (self.show, *self.edit, self.delete, *self.actions) if c]
        return tuple(dict.fromkeys(out))

    def commands(self) -> tuple[str, ...]:
        out = [self.list, *([self.create] if self.create else []), *self.row_commands()]
        return tuple(dict.fromkeys(out))


R = Resource

RESOURCES: tuple[Resource, ...] = (
    # ── Projects & Workspaces ──────────────────────────────────────────────────────────────
    R(
        "project",
        "Projects",
        "Workspaces that own models, pipelines, storage, connections, members and budget.",
        list="project list",
        key="name",
        create="project create",
        show="project show",
        edit=("project set-quota",),
        delete="project delete",
        actions=(
            "project members",
            "project add-member",
            "project remove-member",
            "project assign",
            "project assign-model",
            "project storage",
            "project pipelines",
            "project cost",
            "project budget",
            "project compose",
            "project use",
            "project archive",
        ),
        columns=("name", "status", "description", "cpu_limit", "memory_limit_gb", "gpu_limit"),
    ),
    R(
        "connection",
        "Connections",
        "Named connections (S3, URI, dataplane) a project's code and notebooks resolve by name.",
        list="connection list",
        key="name",
        create="connection create",
        show="connection show",
        delete="connection delete",
        actions=("connection test",),
        columns=("name", "project", "kind", "has_secret", "created_at"),
    ),
    R(
        "dataplane-source",
        "Dataplane sources",
        "External data sources (SQL, object storage, Zenodo, REST, Kafka) pulled into versioned "
        "snapshots (ADR 0130).",
        list="dataplane sources list",
        key="name",
        create="dataplane sources create",
        show="dataplane sources show",
        delete="dataplane sources delete",
        actions=(
            "dataplane test",
            "dataplane preview",
            "dataplane pull",
            "dataplane snapshots",
            "dataplane manifest",
            "dataplane pulls",
        ),
        # `pulls` filters with `--source`, not the key's name, and has no positional to fall back to.
        bind={"dataplane pulls": {"source": "name"}},
        columns=("name", "project", "connector", "connection", "schedule", "enabled", "updated_at"),
    ),
    R(
        "workbench",
        "Workbenches",
        "Per-project notebook servers with the project's connections injected.",
        list="workbench list",
        key="name",
        create="workbench create",
        delete="workbench delete",
        actions=("workbench start", "workbench stop"),
        columns=("name", "project", "status", "image", "cpu", "memory_gb"),
    ),
    R(
        "namespace",
        "Namespaces",
        "Isolation namespaces models are assigned to.",
        list="namespace list",
        key="name",
        create="namespace create",
        show="namespace info",
        actions=("namespace assign",),
        bind={"namespace assign": {"namespace": "name"}},
    ),
    # ── Models & Registry ──────────────────────────────────────────────────────────────────
    R(
        "model",
        "Models",
        "Registered models and their aliases, with lifecycle, cost, lineage and supply-chain actions.",
        list="models list",
        key="Name",
        show="models info",
        actions=(
            "retrain",
            "models cost",
            "models lineage",
            "models rollback history",
            "models rollback run",
            "models card generate",
            "models card history",
            "models diff",
            "models parity",
            "pipeline validate-model",
            "pipeline promote",
            "pipeline quality history",
            "eval history",
            "eval gate show",
            "eval gate set",
            "serve explain history",
            "drift snapshots",
            "drift baseline",
            "fairness report",
            "compliance status",
            "slo burn",
        ),
    ),
    R(
        "approval",
        "Approvals",
        "Sysadmin approval gate for model releases (control plane).",
        list="approvals list",
        key="model_id",
        actions=("approvals approve", "approvals reject"),
        delete="approvals delete",
        bind={"approvals delete": {"approval_id": "id"}},
        columns=("model_id", "status", "commit_sha", "requested_at"),
    ),
    R(
        "adapter",
        "Adapters",
        "Fine-tuned LoRA adapters over a base model (multi-LoRA serving).",
        list="serve adapter list",
        key="adapter_id",
        create="serve adapter add",
        actions=("serve adapter promote", "serve adapter route"),
        bind={"serve adapter route": {"base": "base_ref", "adapter_id": "adapter_id"}},
        columns=("adapter_id", "base_ref", "method", "eval_score", "promoted"),
    ),
    R(
        "engine",
        "Serving engines",
        "Inference engines a model YAML may select.",
        list="models engine list",
        key="engine",
    ),
    R(
        "reproduction",
        "Reproducibility bundles",
        "Signed build manifests that let a model version be rebuilt and verified.",
        list="reproduce list",
        key="model",
        create="reproduce build",
        show="reproduce verify",
        actions=("reproduce run",),
    ),
    # ── Training & Pipelines ───────────────────────────────────────────────────────────────
    R(
        "distributed-run",
        "Distributed training runs",
        "Multi-node training runs with checkpoints and resume.",
        list="pipeline distributed list",
        key="run_id",
        create="pipeline distributed launch",
        show="pipeline distributed status",
        actions=("pipeline distributed resume", "pipeline distributed checkpoint"),
        columns=("run_id", "model", "status", "nodes", "gpus_per_node", "strategy"),
    ),
    # ── Data & Features ────────────────────────────────────────────────────────────────────
    R(
        "dataset-revision",
        "Dataset revisions",
        "Immutable dataset snapshots that training runs can be pinned to.",
        list="data list",
        key="revision_id",
        create="data snapshot",
        actions=("data checkout", "data validate"),
        bind={
            "data checkout": {"dataset": "dataset", "revision_id": "revision_id"},
            "data validate": {"dataset": "dataset", "revision": "revision_id"},
        },
        columns=("revision_id", "dataset", "backend", "row_count", "created_at"),
    ),
    R(
        "feature-view",
        "Feature views",
        "Point-in-time feature views in the feature store.",
        list="feature list",
        key="name",
        create="feature apply",
        show="feature freshness",
        actions=("feature materialize", "feature ingest", "feature get", "feature skew"),
    ),
    R(
        "asset",
        "Data assets",
        "Declared data assets with dependencies and materialisation history.",
        list="assets list",
        key="name",
        create="assets declare",
        show="assets status",
        actions=("assets materialize", "assets source-changed"),
    ),
    # ── Serving & Inference ────────────────────────────────────────────────────────────────
    R(
        "llm-endpoint",
        "LLM endpoints",
        "Launched LLM serving endpoints (vLLM/SGLang, external, compose, HPC).",
        list="serve llm list",
        key="model",
        create="serve llm start",
        show="serve llm status",
        actions=(
            "serve llm health",
            "serve llm args",
            "serve llm bench",
            "serve llm chat",
            "serve llm stop",
        ),
    ),
    R(
        "challenger",
        "Challengers",
        "Champion/challenger mirrors with statistical promotion.",
        list="serve challenger list",
        key="model",
        create="serve challenger enable",
        show="serve challenger status",
        actions=("serve challenger judge", "serve challenger promote", "serve challenger disable"),
        columns=("model", "challenger_version", "mirror_pct", "enabled", "auto_promote"),
    ),
    R(
        "traffic-split",
        "Traffic splits",
        "Per-model traffic weights across Production / Canary / Staging aliases.",
        list="serve traffic-list",
        key="model",
        edit=("serve traffic",),
        columns=("model", "rules", "updated_at", "updated_by"),
    ),
    R(
        "shadow",
        "Shadow deployments",
        "A shadow alias mirrored alongside production, with the comparison log.",
        list="serve shadow status",
        key="Model",
        create="serve shadow enable",
        show="serve shadow log",
        actions=("serve shadow disable",),
    ),
    R(
        "ab-test",
        "A/B tests",
        "Running A/B experiments between model variants, with statistical analysis.",
        list="serve ab status",
        key="model",
        create="serve ab start",
        show="serve ab analyze",
        actions=("serve ab record", "serve ab stop"),
        columns=("model", "name", "variant_a", "variant_b", "split_pct", "status"),
    ),
    R(
        "batch-job",
        "Batch jobs",
        "Offline batch-inference jobs.",
        list="serve batch list",
        key="id",
        create="serve batch submit",
    ),
    R(
        "virtual-key",
        "Gateway keys",
        "Scoped, budgeted virtual keys for the OpenAI-compatible gateway.",
        list="gateway key list",
        key="key_hash",
        create="gateway key issue",
        delete="gateway key revoke",
        columns=("key_hash", "tenant", "project", "budget_usd", "spent_usd", "revoked"),
    ),
    R(
        "tenant-quota",
        "Tenant quotas",
        "Per-tenant request-per-minute limits the serving gateway enforces (via the snapshot).",
        list="gateway quota list",
        key="tenant",
        create="gateway quota set",
        delete="gateway quota remove",
        columns=("tenant", "rpm", "updated_by", "updated_at"),
    ),
    R(
        "knowledge-base",
        "Knowledge bases",
        "RAG knowledge bases, ingested and queried with citations.",
        list="rag list",
        key="kb",
        create="rag ingest",
        actions=("rag query",),
    ),
    R(
        "encoder",
        "Embedding encoders",
        "Registered embedding encoders (name, version, dimension).",
        list="embedding list",
        key="name",
        create="embedding register",
    ),
    # ── GenAI & LLMOps ─────────────────────────────────────────────────────────────────────
    R(
        "prompt",
        "Prompts",
        "Versioned prompt templates with movable labels (dev/prod).",
        list="prompt list",
        key="name",
        create="prompt create",
        show="prompt show",
        actions=("prompt label", "prompt rollback", "prompt diff"),
        bind={"prompt show": {"target": "name"}},
    ),
    # ── Agents & Automation ────────────────────────────────────────────────────────────────
    R(
        "agent-session",
        "Agent sessions",
        "Recorded agent sessions — replay them and look for anomalies.",
        list="agentops sessions",
        key="session_id",
        actions=("agentops replay", "agentops anomalies"),
    ),
    R(
        "memory-review",
        "Memory reviews",
        "Agent procedures awaiting human approval before they are remembered.",
        list="agent memory review list",
        key="id",
        actions=("agent memory review approve", "agent memory review reject"),
        bind={
            "agent memory review approve": {"review_id": "id"},
            "agent memory review reject": {"review_id": "id"},
        },
    ),
    R(
        "mcp-tool",
        "MCP tools",
        "Tools the MCP server exposes to agents.",
        list="mcp tools",
        key="Tool",
    ),
    # ── Monitoring & Quality ───────────────────────────────────────────────────────────────
    R(
        "slo",
        "SLOs",
        "Model-quality SLOs with error budgets and burn-rate alerts.",
        list="slo list",
        key="name",
        create="slo set",
        show="slo status",
        edit=("slo set",),
        actions=("slo burn", "slo record", "slo generate"),
        # An SLO is (model, name); `slo burn` takes only the model, which the default would fill
        # with the SLO's *name*.
        bind={"slo burn": {"model": "model"}},
        columns=("model", "name", "target", "window", "sli_source", "gate_promotion"),
    ),
    R(
        "auto-retrain",
        "Auto-retrain policies",
        "Drift thresholds that trigger a retrain automatically, with cooldowns.",
        list="drift auto-retrain status",
        key="model",
        create="drift auto-retrain enable",
        edit=("drift auto-retrain enable",),
        actions=(
            "drift auto-retrain disable",
            "drift snapshots",
            "drift baseline",
            "drift forecast",
        ),
        columns=("model", "enabled", "min_z_score", "dataset_name", "cooldown_s", "last_triggered"),
    ),
    R(
        "judge-calibration",
        "Judge calibrations",
        "Measured LLM-judge calibrations (no uncalibrated judge may gate).",
        list="eval calibration list",
        key="judge",
        create="eval calibrate",
        show="eval calibration show",
    ),
    # ── HPC, Fleet & FinOps ────────────────────────────────────────────────────────────────
    R(
        "cluster",
        "HPC clusters",
        "Registered scheduler clusters and their approval state.",
        list="hpc clusters",
        key="name",
        create="hpc connect",
        actions=("hpc approve", "hpc reject", "hpc preflight", "hpc queue", "hpc nodes"),
        bind={"hpc queue": {"cluster": "name"}, "hpc nodes": {"cluster": "name"}},
        columns=("name", "scheduler", "state", "host", "approved_by"),
    ),
    R(
        "hpc-job",
        "HPC jobs",
        "Training jobs submitted to HPC schedulers, with GPU-hour cost links.",
        list="hpc jobs",
        key="job_id",
    ),
    R(
        "budget",
        "Project budgets",
        "GPU-hour and cost budgets per project, with consumption and breach status.",
        list="finops budget status",
        key="project",
        create="finops budget set",
        edit=("finops budget set",),
        actions=("project cost",),
        columns=(
            "project",
            "status",
            "gpu_hours_used",
            "gpu_hours_budget",
            "gpu_pct",
            "cost_used_usd",
            "cost_budget_usd",
            "cost_pct",
        ),
    ),
    R(
        "device-pool",
        "Device pools",
        "Heterogeneous accelerator pools for placement and governed cloud burst.",
        list="hardware pools",
        key="name",
        create="hardware add-pool",
        columns=("name", "accelerator", "count", "target", "region", "status"),
    ),
    R(
        "placement",
        "Placement decisions",
        "Recorded hardware placement decisions.",
        list="hardware decisions",
        key="id",
    ),
    # ── Governance & Security ──────────────────────────────────────────────────────────────
    R(
        "ai-system",
        "AI systems (EU AI Act)",
        "The register of AI systems: risk tier, intended purpose and conformity state per model.",
        list="compliance status",
        key="model",
        create="compliance classify",
        edit=("compliance classify", "compliance declare"),
        actions=("compliance art12", "compliance technical-file", "compliance declaration"),
        columns=("model", "risk_tier", "conformity_state", "intended_purpose", "in_scope"),
    ),
    R(
        "audit-review",
        "Audit reviews",
        "Recorded human reviews of sampled audit-chain ranges.",
        list="audit reviews",
        key="event_id",
        create="audit review",
        columns=("event_id", "ts", "reviewer", "from_id", "to_id", "notes"),
    ),
    R(
        "secret",
        "Secrets",
        "Platform secrets — metadata only; values are never shown.",
        list="secrets list",
        key="path",
        create="secrets set",
        edit=("secrets set",),
        actions=("secrets rotate",),
    ),
    R(
        "policy",
        "Policies",
        "Policy-as-code rules that can veto mutations.",
        list="policy list",
        key="name",
        rows_key="policies",
    ),
    R(
        "policy-bundle",
        "Policy bundles",
        "Signed policy bundles per tenant.",
        list="policy bundle list",
        key="version",
        create="policy bundle sign",
        show="policy bundle verify",
    ),
    R(
        "provider",
        "Calculation providers",
        "Pluggable formulas for carbon, cost, placement, drift, promotion and LLM domains.",
        list="providers list",
        key="name",
        create="providers author",
        show="providers show",
        delete="providers rm",
        actions=("providers activate",),
        columns=("domain", "name", "kind", "default", "ok"),
    ),
    R(
        "audit-checkpoint",
        "Audit checkpoints",
        "Signed checkpoints over the audit hash chain.",
        list="audit checkpoints",
        key="id",
        create="audit checkpoint",
    ),
    # ── Platform & Integrations ────────────────────────────────────────────────────────────
    R(
        "backup",
        "Backups",
        "Single-database snapshots — verify or restore them.",
        list="backup list",
        key="path",
        rows_key="snapshots",
        create="backup create",
        show="backup verify",
        actions=("backup restore",),
        columns=("file", "created_at", "size_bytes", "has_manifest"),
    ),
    R(
        "backup-bundle",
        "Backup bundles",
        "Multi-tier platform bundles (datastores, config, objects).",
        list="backup list",
        key="path",
        rows_key="bundles",
        create="backup create",
        show="backup verify-bundle",
        actions=("backup restore-bundle",),
        bind={
            "backup verify-bundle": {"bundle_dir": "path"},
            "backup restore-bundle": {"bundle_dir": "path"},
        },
    ),
    R(
        "bus-model",
        "Dataplane bus models",
        "Per-model Dataplane bus request/response UUIDs.",
        list="dataplane-bus list",
        key="Model",
        actions=("dataplane-bus regen-uuid",),
    ),
    R(
        "config-context",
        "Config contexts",
        "Named CLI configuration contexts (environments) and the active one.",
        list="config contexts",
        key="name",
        rows_key="contexts",
        actions=("config use",),
    ),
    R(
        "control-command",
        "Control-plane commands",
        "Asynchronous control-plane commands (e.g. `exa retrain --async`): state, attempts, cancel.",
        list="commands list",
        key="command_id",
        rows_key="items",
        show="commands show",
        actions=("commands cancel",),
        columns=("command_id", "kind", "state", "attempts", "updated_at"),
    ),
    R(
        "module",
        "Modules",
        "This site's feature profile: which platform modules are on, and why (ADR 0128).",
        list="modules list",
        key="module",
        show="modules show",
        actions=("modules enable", "modules disable"),
        columns=("module", "title", "enabled", "why", "requires"),
    ),
)

# Command groups that have a `list`-like verb but are not resources, and why. Keeps the coverage
# check honest: a new list command must be placed in one of the two.
NOT_RESOURCES: dict[str, str] = {
    "offline list": "Offline jobs are created by `exa offline run` over a model version and a "
    "pinned dataset (a run, not a record you fill in); `exa offline status/cancel` and "
    "`exa ops status` follow one - the CLI console is the surface.",
    "ops list": "Operations are the control plane's command records, created by the calls that "
    "start long-running work (`exa retrain`), never by a create form; `exa ops status/wait/cancel` "
    "follow one handle - the Commands view is the read surface.",
    "plan list": "Agent plans are created and applied through the MCP plan_change/apply_plan tools "
    "and are single-use; `exa plan show` reads one — there is nothing to create or edit here.",
    "commands list": "Asynchronous command records are created by `exa retrain --async`, not by a "
    "create form, and are read-only apart from cancel — `exa commands show/cancel` covers them.",
    "features list": "Feature *files* per model, fetched and pushed as files — the workspace covers it.",
    "pipeline list": "The pipeline registry view is the Pipelines console; its rows are not addressable.",
    "agent memory list": "Requires a memory kind to list; it is a query, not an enumerable resource.",
    "models cost-list": "A report across models, not a collection with its own actions.",
    "hardware portable": "A compatibility query (engine × accelerator), not a collection.",
    "finops carbon policy list": "Evaluation history read back from the audit chain — records, not "
    "managed objects; `finops carbon policy status` answers per policy.",
}


# ── binding ───────────────────────────────────────────────────────────────────────────────


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def key_param(resource: Resource, command: dict[str, Any]) -> str | None:
    """The parameter of ``command`` that receives the row's key."""
    explicit = resource.bind.get(command["path"], {})
    for param, row_field in explicit.items():
        if _norm(row_field) == _norm(resource.key):
            return param
    params = [p for p in command["params"] if not p.get("blocked") and not p.get("implied")]
    for p in params:
        if _norm(p["name"]) == _norm(resource.key):
            return str(p["name"])
    positional = [p for p in params if p["kind"] == "argument"]
    return str(positional[0]["name"]) if positional else None


def build_resources(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """The resources as JSON for the catalog, with every row command's key binding resolved.

    ``bindings[command]`` is ``{param: row_field}``; the UI additionally pre-fills any other
    parameter named like a row field. Resources whose list command is missing from this CLI
    (e.g. an optional group not installed) are left out rather than rendered broken.
    """
    by_path = {c["path"]: c for c in catalog["commands"]}
    out: list[dict[str, Any]] = []
    for r in RESOURCES:
        if r.list not in by_path:
            continue
        bindings: dict[str, dict[str, str]] = {}
        for path in r.row_commands():
            cmd = by_path.get(path)
            if cmd is None:
                continue
            binding = dict(r.bind.get(path, {}))
            kp = key_param(r, cmd)
            if kp and kp not in binding:
                binding[kp] = r.key
            bindings[path] = binding
        out.append(
            {
                "id": r.id,
                "title": r.title,
                "description": r.description,
                "panel": by_path[r.list]["panel"],
                "list": r.list,
                "key": r.key,
                "rows_key": r.rows_key,
                "create": r.create if r.create in by_path else None,
                "show": r.show if r.show in by_path else None,
                "edit": [c for c in r.edit if c in by_path],
                "delete": r.delete if r.delete in by_path else None,
                "actions": [c for c in r.actions if c in by_path],
                "bindings": bindings,
                "columns": list(r.columns),
                "tiers": {c: by_path[c]["tier"] for c in r.commands() if c in by_path},
            }
        )
    return out


def rows_of(parsed: Any, resource: dict[str, Any]) -> list[dict[str, Any]]:
    """The item rows in a list command's JSON output (mirrored by the UI's ``rowsOf``).

    A list of records is used as is; a list of bare values becomes ``{key: value}`` rows (`exa
    prompt list` prints names); an object yields its ``rows_key`` array, or its only array.
    """
    data = parsed
    if isinstance(data, dict):
        if resource.get("rows_key"):
            data = data.get(resource["rows_key"], [])
        else:
            arrays = [v for v in data.values() if isinstance(v, list)]
            data = arrays[0] if len(arrays) == 1 else []
    if not isinstance(data, list):
        return []
    return [r if isinstance(r, dict) else {resource["key"]: r} for r in data]


def row_value(row: dict[str, Any], field_name: str) -> Any:
    """A row field looked up case- and dash-insensitively (`Name` vs `name`)."""
    wanted = _norm(field_name)
    for k, v in row.items():
        if _norm(k) == wanted:
            return v
    return None


def fits(param: dict[str, Any], value: Any) -> bool:
    """Whether a row value is usable as ``param`` — a display string like ``"0 / —"`` must not
    pre-fill a number field (it would only come back as a validation error)."""
    if value is None or isinstance(value, (dict, list)):
        return False
    text = str(value).strip()
    if param["type"] == "int":
        return not isinstance(value, bool) and text.lstrip("-").isdigit()
    if param["type"] == "float":
        try:
            float(text)
        except ValueError:
            return False
        return not isinstance(value, bool)
    if param["type"] == "choice":
        return text in param.get("choices", [])
    return True


def prefill(
    resource: dict[str, Any], command: dict[str, Any], row: dict[str, Any]
) -> dict[str, Any]:
    """Form values for running ``command`` on ``row``: the resolved bindings, then any parameter
    named like a row field — each only when the value fits the parameter's type (mirrored by
    the UI's ``prefillFromRow``)."""
    specs = {p["name"]: p for p in command["params"]}
    values: dict[str, Any] = {}
    for param, row_field in resource["bindings"].get(command["path"], {}).items():
        value = row_value(row, row_field)
        if param in specs and fits(specs[param], value):
            values[param] = value
    for p in command["params"]:
        if p["name"] in values or p.get("blocked") or p.get("implied") or p.get("flag"):
            continue
        value = row_value(row, p["name"])
        if fits(p, value):
            values[p["name"]] = value
    return values


def coverage(catalog: dict[str, Any]) -> dict[str, Any]:
    """How much of the CLI the resources reach (the rest is the CLI Console's)."""
    runnable = {c["path"] for c in catalog["commands"] if c["tier"] != surface.CLI_ONLY}
    reached = {c for r in RESOURCES for c in r.commands()} & runnable
    return {"resources": len(RESOURCES), "commands": len(reached), "runnable": len(runnable)}
