"""Site feature profiles — which ExaMLOps modules a centre runs (ADR 0128).

A *module* is a coarse, centre-meaningful slice of the platform: "HPC fleet", "GenAI & LLMOps",
"the Skipper agent". One centre runs GPU clusters and wants the HPC module; another has no LLM
endpoint and must not show the agent; a third is a pure serving site. Before this module there
were five unrelated switches (dashboard flags, a dozen ``*_ENABLED`` env vars, Compose profiles,
Helm ``enabled:`` keys, policy files) and none of them was scoped to a site.

The catalog below is the single place that ties a module to every surface it touches:

* its root ``exa`` commands (hidden and refused when the module is off),
* its dashboard flags and API route prefixes (flags evaluate false, routes answer 404),
* its always-on Compose services and opt-in Compose profiles, and its Helm switches — which
  ``exa modules render`` turns into a Compose override / Helm values for the deployment layer.

**Resolution** (later layers override earlier ones)::

    preset            the site profile's ``preset`` (default ``full`` — everything, as before)
    site profile      ``enable`` / ``disable`` lists in ``site.toml``
    EXAMLOPS_FEATURES an overlay: ``preset:<name>``, ``+module`` / ``module``, ``-module``

then ``core`` is forced on and dependencies are closed: an explicitly enabled module pulls in what
it needs unless that was explicitly disabled, in which case the dependent is switched off too and
the reason says so. With no site file and no env the profile is ``full``, so an existing install
behaves exactly as before.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FEATURES_ENV = "EXAMLOPS_FEATURES"
SITE_PROFILE_ENV = "EXAMLOPS_SITE_PROFILE"
SITE_FILE = "site.toml"
DEFAULT_PRESET = "full"

# The inactive Compose profile a rendered override parks disabled always-on services in.
COMPOSE_DISABLED_PROFILE = "module-disabled"


@dataclass(frozen=True)
class Module:
    id: str
    title: str
    description: str
    cli: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    required: bool = False
    dashboard_flags: tuple[str, ...] = ()
    dashboard_api: tuple[str, ...] = ()
    # Dashboard page routes (frontend paths) the module owns — hidden from the navigation when off.
    dashboard_pages: tuple[str, ...] = ()
    # MCP tool tags (``examlops.mcp.tools`` ToolSpec.tags) of this module's domain: a tool carrying
    # one of them is not offered to agents while the module is off.
    mcp_tags: tuple[str, ...] = ()
    compose_services: tuple[str, ...] = ()
    compose_profiles: tuple[str, ...] = ()
    helm: tuple[str, ...] = ()
    env_switches: tuple[str, ...] = ()
    needs: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


M = Module

#: Every module, in the order ``exa modules list`` shows them. Every root ``exa`` command belongs to
#: exactly one module — ``tests/unit/test_lifecycle_modules.py`` fails when a new command is not
#: placed, so a feature can never ship without an on/off switch.
CATALOG: tuple[Module, ...] = (
    M(
        "core",
        "Platform core",
        "Status, configuration, the model registry, projects, approvals, audit, secrets, policy, "
        "backup, upgrades and this module switch itself. Always on.",
        cli=(
            "status",
            "doctor",
            "explain",
            "env",
            "docs",
            "config",
            "plugins",
            "models",
            "approvals",
            "auth",
            "audit",
            "secrets",
            "policy",
            "providers",
            "project",
            "namespace",
            "connection",
            "stack",
            "backup",
            "events",
            "admission",
            "instance",
            "upgrade",
            "modules",
        ),  # fmt: skip
        required=True,
        dashboard_flags=("commandPalette", "projectsConsole", "mlopsConsole"),
    ),
    M(
        "training",
        "Training & data",
        "Prefect training pipelines, retraining, scaffolding, reproducibility, dataset "
        "versioning, the feature store and asset-centric pipelines.",
        cli=(
            "pipeline",
            "retrain",
            "retrain-status",
            "commands",
            "scaffold",
            "reproduce",
            "data",
            "dataplane",
            "feature",
            "features",
            "assets",
        ),  # fmt: skip
        dashboard_api=("/pipelines", "/scaffold", "/hpo", "/feature-store", "/features"),
        dashboard_pages=("/build/datasets", "/build/features", "/build/assets", "/build/pipelines"),
        mcp_tags=("training", "data"),
    ),
    M(
        "serving",
        "Serving & inference",
        "Ray Serve multi-model serving, predictions, production verification, traffic splits, "
        "shadow/A-B/champion-challenger, batch inference, autoscaling and rollback.",
        cli=("serve", "predict", "production"),
        dashboard_api=(
            "/shadow",
            "/ab-testing",
            "/batch",
            "/v1/traffic",
            "/v1/scaling",
            "/challenger",
            "/rollback",
            "/explain",
        ),  # fmt: skip
        dashboard_pages=("/serve/traffic", "/serve/scaling"),
        mcp_tags=("serving",),
    ),
    M(
        "quality",
        "Model quality monitoring",
        "Prediction and input drift, continuous evaluation and judge calibration, model SLOs "
        "and fairness gates.",
        cli=("drift", "eval", "slo", "fairness"),
        dashboard_flags=("incidentTimeline",),
        dashboard_api=("/drift", "/slo", "/fairness", "/quality"),
        env_switches=("EXAMLOPS_SLO_GATE_ENABLED", "EXAMLOPS_FAIRNESS_GATE_ENABLED"),
        dashboard_pages=("/operate/drift", "/operate/slos", "/govern/fairness"),
        mcp_tags=("drift", "eval", "quality"),
    ),
    M(
        "governance",
        "Governance & compliance",
        "Compliance evidence, governance reports and model/dataset cards.",
        cli=("compliance", "governance", "cards"),
        dashboard_api=("/compliance", "/v1/governance", "/cards"),
        dashboard_pages=("/govern/governance", "/govern/compliance"),
    ),
    M(
        "genai",
        "GenAI & LLMOps",
        "The OpenAI-compatible gateway, prompt registry, guardrails, vector store, RAG, "
        "embeddings and fine-tuning.",
        cli=(
            "genai",
            "prompt",
            "guardrails",
            "gateway",
            "vector",
            "rag",
            "embedding",
            "finetune",
        ),  # fmt: skip
        dashboard_flags=("llmopsConsole",),
        dashboard_api=("/v1/llmops", "/gateway", "/prompts"),
        env_switches=("EXAMLOPS_GUARDRAIL_MODE",),
        dashboard_pages=("/build/prompts", "/serve/llmops", "/serve/gateway"),
        mcp_tags=("gateway", "llmops"),
    ),
    M(
        "llm-serving",
        "LLM engine serving (vLLM)",
        "A GPU vLLM engine server behind the gateway.",
        requires=("genai",),
        compose_profiles=("vllm",),
        needs="an NVIDIA GPU on the Docker host",
    ),
    M(
        "agent",
        "Skipper agent & MCP",
        "The Skipper management agent (ask/chat, dashboard copilot), agent operations and the "
        "MCP / A2A surface for external agents.",
        cli=("ask", "chat", "agent", "agentops", "mcp"),
        dashboard_api=("/v1/copilot",),
        compose_services=("agent",),
        helm=("agent.enabled",),
        env_switches=("AGENT_MEMORY_ENABLED", "EXAMLOPS_MCP_ALLOW_WRITES"),
        needs="an LLM endpoint (Ollama or any OpenAI-compatible API)",
    ),
    M(
        "autopilot",
        "Self-driving autopilot",
        "The closed loop detect → retrain → validate → promote, under policy gates.",
        cli=("autopilot",),
        requires=("quality", "training", "serving"),
        dashboard_api=("/autopilot",),
        env_switches=("EXAMLOPS_AUTOPILOT_ENABLED",),
        dashboard_pages=("/operate/autopilot",),
    ),
    M(
        "hpc",
        "HPC fleet",
        "Scheduler discovery and approval (Slurm, Flux), placement, capacity, hardware "
        "portability, the fleet twin and federated training.",
        cli=("hpc", "fleet", "hardware", "federated"),
        dashboard_flags=("facilityConsole",),
        dashboard_api=("/v1/facility",),
        env_switches=("EXAMLOPS_HPC_SCHEDULER", "EXAMLOPS_SLURM_MODE"),
        dashboard_pages=("/operate/facility",),
        mcp_tags=("hpc", "fleet"),
    ),
    M(
        "finops",
        "FinOps & Green-AI",
        "Budgets, cost and carbon accounting, and offline cost/carbon/SLA reports.",
        cli=("finops", "report"),
        dashboard_api=("/v1/finops",),
        dashboard_pages=("/operate/finops",),
        mcp_tags=("finops",),
    ),
    M(
        "workbenches",
        "Workbenches (JupyterHub)",
        "Per-project notebook workbenches spawned by JupyterHub.",
        cli=("workbench",),
        dashboard_api=("/v1/workbenches",),
        compose_profiles=("jupyter",),
        dashboard_pages=("/platform/jupyter",),
    ),
    M(
        "observability",
        "Observability stack",
        "Prometheus, Alertmanager, Grafana, Loki and Tempo for the platform's own telemetry.",
        compose_profiles=("monitoring",),
        env_switches=("OTEL_SDK_DISABLED",),
    ),
    M(
        "integrations",
        "External integrations",
        "The Dataplane bus bridge, ModelZoo repository sync and signed Exchange packages.",
        cli=("dataplane-bus", "modelzoo", "exchange"),
        requires=("serving",),
        dashboard_api=("/modelzoo",),
        compose_profiles=("dataplane-bus",),
        needs="a sibling dataplane-bus checkout to build the bridge image",
        mcp_tags=("modelzoo",),
    ),
)

_BY_ID: dict[str, Module] = {m.id: m for m in CATALOG}

_STANDARD = (
    "core",
    "training",
    "serving",
    "quality",
    "governance",
    "observability",
    "workbenches",
    "finops",
)

#: Named starting points for a site profile: name → (description, modules).
PRESETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "full": ("Every module — the behaviour of an install with no site profile.", tuple(_BY_ID)),
    "standard": ("A typical MLOps centre: train, serve, monitor, govern.", _STANDARD),
    "minimal": ("Just train and serve models.", ("core", "training", "serving")),
    "hpc-center": (
        "An HPC centre: the standard set plus the HPC fleet and the autopilot.",
        (*_STANDARD, "hpc", "autopilot"),
    ),
    "genai": (
        "An LLM platform: the standard set plus GenAI, vLLM serving and the agent.",
        (*_STANDARD, "genai", "llm-serving", "agent"),
    ),
}


def module(module_id: str) -> Module:
    """The catalog entry for ``module_id`` (``KeyError`` with the known ids otherwise)."""
    try:
        return _BY_ID[module_id]
    except KeyError:
        raise KeyError(f"unknown module {module_id!r}; known: {', '.join(_BY_ID)}") from None


def module_ids() -> list[str]:
    return list(_BY_ID)


# ── the site profile file ─────────────────────────────────────────────────────────────────


def site_profile_path() -> tuple[Path, str]:
    """Where the site profile lives, and what chose it.

    ``EXAMLOPS_SITE_PROFILE`` wins, then ``<EXAMLOPS_DATA_DIR>/site.toml`` (the profile is
    instance data: backed up with it, kept across upgrades), then the site configuration
    directory (``EXAMLOPS_CONFIG_DIR`` or ``~/.config/examlops``).
    """
    if raw := os.getenv(SITE_PROFILE_ENV, "").strip():
        return Path(raw).expanduser(), SITE_PROFILE_ENV
    from examlops.lifecycle.datadir import config_dir, data_path

    if (p := data_path(SITE_FILE)) is not None:
        return p, "data-root"
    return config_dir() / SITE_FILE, "default"


@dataclass
class SiteFile:
    path: str
    exists: bool
    name: str = ""
    preset: str | None = None
    enable: list[str] = field(default_factory=list)
    disable: list[str] = field(default_factory=list)
    error: str | None = None


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def read_site_file(path: Path | None = None) -> SiteFile:
    """Parse the site profile. A missing file is not an error; a malformed one is reported."""
    p = path or site_profile_path()[0]
    if not p.is_file():
        return SiteFile(str(p), False)
    try:
        data = tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return SiteFile(str(p), True, error=f"unreadable site profile: {exc}")
    site = data.get("site", {}) if isinstance(data.get("site"), dict) else {}
    feats = data.get("features", {}) if isinstance(data.get("features"), dict) else {}
    preset = feats.get("preset")
    return SiteFile(
        str(p),
        True,
        name=str(site.get("name", "")),
        preset=str(preset) if preset else None,
        enable=_str_list(feats.get("enable")),
        disable=_str_list(feats.get("disable")),
    )


_HEADER = (
    "# ExaMLOps site profile — which modules this centre runs (ADR 0128).\n"
    "# Edit with `exa modules enable|disable|preset`; inspect with `exa modules list`.\n"
    "# EXAMLOPS_FEATURES (e.g. 'preset:standard,+hpc,-finops') overlays this file.\n\n"
)


def write_site_file(sf: SiteFile, path: Path | None = None) -> Path:
    """Persist ``sf`` as TOML (atomic replace). Unknown modules are refused before writing."""
    import tomli_w

    for mid in [*sf.enable, *sf.disable]:
        module(mid)
    if sf.preset is not None and sf.preset not in PRESETS:
        raise KeyError(f"unknown preset {sf.preset!r}; known: {', '.join(PRESETS)}")
    p = path or Path(sf.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {}
    if sf.name:
        doc["site"] = {"name": sf.name}
    feats: dict[str, Any] = {}
    if sf.preset:
        feats["preset"] = sf.preset
    feats["enable"] = sorted(dict.fromkeys(sf.enable))
    feats["disable"] = sorted(dict.fromkeys(sf.disable))
    doc["features"] = feats
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(_HEADER + tomli_w.dumps(doc))
    tmp.replace(p)
    return p


# ── EXAMLOPS_FEATURES ─────────────────────────────────────────────────────────────────────


@dataclass
class Spec:
    preset: str | None = None
    enable: list[str] = field(default_factory=list)
    disable: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def parse_spec(text: str) -> Spec:
    """Parse ``preset:<name>,+mod,mod,-mod`` (commas or whitespace). Unknown ids are collected."""
    spec = Spec()
    for raw in text.replace(",", " ").split():
        token = raw.strip()
        if not token:
            continue
        if token.startswith("preset:"):
            spec.preset = token.split(":", 1)[1].strip()
            continue
        sign, name = ("-", token[1:]) if token.startswith("-") else ("+", token.lstrip("+"))
        if name not in _BY_ID:
            spec.unknown.append(token)
            continue
        (spec.disable if sign == "-" else spec.enable).append(name)
    return spec


# ── resolution ────────────────────────────────────────────────────────────────────────────


@dataclass
class Profile:
    preset: str
    enabled: dict[str, bool]
    reasons: dict[str, str]
    sources: list[str]
    warnings: list[str]
    site_name: str = ""
    site_file: str = ""
    explicit_enable: list[str] = field(default_factory=list)
    explicit_disable: list[str] = field(default_factory=list)

    def is_enabled(self, module_id: str) -> bool:
        return self.enabled.get(module_id, True)

    def enabled_ids(self) -> list[str]:
        return [m for m, on in self.enabled.items() if on]

    def disabled_ids(self) -> list[str]:
        return [m for m, on in self.enabled.items() if not on]

    def spec(self) -> str:
        """The profile as one ``EXAMLOPS_FEATURES`` value, relative to its preset."""
        base = set(PRESETS[self.preset][1])
        plus = [m for m in _BY_ID if self.enabled[m] and m not in base]
        minus = [m for m in _BY_ID if not self.enabled[m] and m in base]
        return ",".join(
            [f"preset:{self.preset}", *(f"+{m}" for m in plus), *(f"-{m}" for m in minus)]
        )

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["spec"] = self.spec()
        return out


def resolve(*, site: SiteFile | None = None, env: Mapping[str, str] | None = None) -> Profile:
    """The effective profile for this process (see the module docstring for the layering)."""
    environ = os.environ if env is None else env
    sf = site if site is not None else read_site_file()
    es = parse_spec(environ.get(FEATURES_ENV, ""))
    warnings: list[str] = []
    sources: list[str] = []
    if sf.error:
        warnings.append(sf.error)
    if sf.exists and not sf.error:
        sources.append(f"site profile {sf.path}")
    if environ.get(FEATURES_ENV, "").strip():
        sources.append(FEATURES_ENV)
    warnings.extend(f"{FEATURES_ENV}: unknown module {u!r}" for u in es.unknown)

    preset = es.preset or sf.preset or DEFAULT_PRESET
    if preset not in PRESETS:
        warnings.append(f"unknown preset {preset!r} — using {DEFAULT_PRESET!r}")
        preset = DEFAULT_PRESET
    enabled = {m: m in PRESETS[preset][1] for m in _BY_ID}
    reasons = {m: f"preset {preset}" for m in _BY_ID}

    explicit_on: list[str] = []
    explicit_off: list[str] = []

    def _layer(on: Iterable[str], off: Iterable[str], label: str) -> None:
        for mid in on:
            if mid not in _BY_ID:
                warnings.append(f"{label}: unknown module {mid!r}")
                continue
            enabled[mid] = True
            reasons[mid] = f"enabled by {label}"
            explicit_on.append(mid)
            if mid in explicit_off:
                explicit_off.remove(mid)
        for mid in off:
            if mid not in _BY_ID:
                warnings.append(f"{label}: unknown module {mid!r}")
                continue
            enabled[mid] = False
            reasons[mid] = f"disabled by {label}"
            explicit_off.append(mid)
            if mid in explicit_on:
                explicit_on.remove(mid)

    _layer(sf.enable, sf.disable, "site profile")
    _layer(es.enable, es.disable, FEATURES_ENV)

    for m in CATALOG:
        if m.required and not enabled[m.id]:
            warnings.append(f"module {m.id!r} is required and cannot be disabled")
            enabled[m.id] = True
            reasons[m.id] = "required"
        elif m.required:
            reasons[m.id] = "required"

    # Dependency closure to a fixed point: pull deps in, unless explicitly disabled.
    changed = True
    while changed:
        changed = False
        for m in CATALOG:
            if not enabled[m.id]:
                continue
            for dep in m.requires:
                if enabled[dep]:
                    continue
                if dep in explicit_off:
                    enabled[m.id] = False
                    reasons[m.id] = f"off: requires {dep!r}, which is disabled"
                    if m.id in explicit_on:
                        warnings.append(f"{m.id!r} was enabled but requires disabled {dep!r}")
                else:
                    enabled[dep] = True
                    reasons[dep] = f"required by {m.id!r}"
                changed = True
                break

    return Profile(
        preset=preset,
        enabled=enabled,
        reasons=reasons,
        sources=sources or ["default (no site profile)"],
        warnings=warnings,
        site_name=sf.name,
        site_file=sf.path,
        explicit_enable=explicit_on,
        explicit_disable=explicit_off,
    )


# ── lookups used by the surfaces ──────────────────────────────────────────────────────────

_CLI_OWNER: dict[str, str] = {c: m.id for m in CATALOG for c in m.cli}
_FLAG_OWNER: dict[str, str] = {f: m.id for m in CATALOG for f in m.dashboard_flags}
_API_PREFIXES: list[tuple[str, str]] = sorted(
    ((p, m.id) for m in CATALOG for p in m.dashboard_api), key=lambda t: -len(t[0])
)


def module_for_command(name: str) -> str | None:
    """The module that owns root command ``name`` (``None`` for plugins and unknown names)."""
    return _CLI_OWNER.get(name)


def module_for_flag(flag: str) -> str | None:
    return _FLAG_OWNER.get(flag)


_TAG_OWNER: dict[str, str] = {t: m.id for m in CATALOG for t in m.mcp_tags}


def modules_for_tags(tags: Iterable[str]) -> list[str]:
    """The modules an MCP tool belongs to, from its tags (a tool can span two domains)."""
    return sorted({_TAG_OWNER[t] for t in tags if t in _TAG_OWNER})


def module_for_api_path(path: str) -> str | None:
    """The module owning a dashboard API path (``/api`` prefix optional; segment-boundary match)."""
    rel = path[4:] if path.startswith("/api/") else path
    for prefix, owner in _API_PREFIXES:
        if rel == prefix or rel.startswith(prefix + "/"):
            return owner
    return None


# ── mutations (write the site profile) ────────────────────────────────────────────────────


def set_modules(*, enable: Iterable[str] = (), disable: Iterable[str] = ()) -> SiteFile:
    """Enable/disable modules in the site profile; returns the written profile file."""
    sf = read_site_file()
    if sf.error:
        raise ValueError(sf.error)
    for mid in enable:
        module(mid)
        sf.enable = [*[m for m in sf.enable if m != mid], mid]
        sf.disable = [m for m in sf.disable if m != mid]
    for mid in disable:
        if module(mid).required:
            raise ValueError(f"module {mid!r} is required and cannot be disabled")
        sf.disable = [*[m for m in sf.disable if m != mid], mid]
        sf.enable = [m for m in sf.enable if m != mid]
    write_site_file(sf)
    return sf


def set_preset(name: str, *, keep_overrides: bool = True, site_name: str | None = None) -> SiteFile:
    if name not in PRESETS:
        raise KeyError(f"unknown preset {name!r}; known: {', '.join(PRESETS)}")
    sf = read_site_file()
    if sf.error:
        raise ValueError(sf.error)
    sf.preset = name
    if not keep_overrides:
        sf.enable, sf.disable = [], []
    if site_name is not None:
        sf.name = site_name
    write_site_file(sf)
    return sf


def reset_site_file() -> str | None:
    """Delete the site profile (back to ``full``). Returns the removed path, if any."""
    p, _ = site_profile_path()
    if p.is_file():
        p.unlink()
        return str(p)
    return None


# ── rendering for the deployment layer ────────────────────────────────────────────────────


def render_env(profile: Profile) -> dict[str, str]:
    return {FEATURES_ENV: profile.spec()}


def render_compose(profile: Profile) -> dict[str, Any]:
    """Compose inputs for ``profile``: ``COMPOSE_PROFILES`` + an override for always-on services.

    Disabled always-on services are parked in an inactive profile (a Compose override may add
    ``profiles:`` to a service), and a dependency on them is made optional (``required: false``,
    Compose ≥ 2.20) so the services that remain still start.
    """
    profiles: list[str] = []
    notes: list[str] = []
    services: dict[str, Any] = {}
    for m in CATALOG:
        if profile.is_enabled(m.id):
            profiles.extend(p for p in m.compose_profiles if p not in profiles)
            if m.needs and m.compose_profiles:
                notes.append(f"{m.id}: needs {m.needs}")
        else:
            for svc in m.compose_services:
                services[svc] = {"profiles": [COMPOSE_DISABLED_PROFILE]}
    if "agent" in services:
        # The dashboard waits for a healthy agent; without one it must still start.
        services["dashboard"] = {
            "depends_on": {"agent": {"condition": "service_healthy", "required": False}}
        }
    override = {"services": services} if services else None
    return {
        "env": {"COMPOSE_PROFILES": ",".join(profiles), **render_env(profile)},
        "override": override,
        "notes": notes,
    }


def render_helm(profile: Profile) -> dict[str, Any]:
    """Helm values for ``profile``: the features string and the tiers the chart can switch off."""
    return {
        "site": {"features": profile.spec()},
        "agent": {"enabled": profile.is_enabled("agent")},
    }
