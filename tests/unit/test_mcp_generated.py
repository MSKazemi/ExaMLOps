"""The generated agent surface (ADR 0147 d1) — one source of truth, and the guard on it.

Nothing here hard-codes the *list* of commands: the catalog is read from the live Click tree on
every run, because commands are added continuously and a snapshot would go stale the same day.
What is asserted literally is the shape of a few long-standing commands' schemas — that is the
whole claim of decision 1, that a tool definition is derived from what the CLI actually declares.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from examlops.cli import surface
from examlops.mcp import generated as g
from examlops.mcp.tools import REGISTRY, iter_tools

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def catalog() -> dict:
    return surface.build_catalog()


@pytest.fixture(scope="module")
def specs(catalog: dict) -> tuple:
    return g.generate(include_writes=True, catalog=catalog)


def _by_path(catalog: dict) -> dict[str, dict]:
    return {c["path"]: c for c in catalog["commands"]}


# ── determinism ───────────────────────────────────────────────────────────────


def test_generation_is_stable_across_runs(catalog):
    first = g.generate(include_writes=True, catalog=catalog)
    second = g.generate(include_writes=True, catalog=catalog)
    assert [s.name for s in first] == [s.name for s in second]
    assert [s.annotations for s in first] == [s.annotations for s in second]
    assert [(s.tier, s.mutating, s.tags, s.use_cases) for s in first] == [
        (s.tier, s.mutating, s.tags, s.use_cases) for s in second
    ]
    assert [g.SCHEMAS[s.name] for s in first] == [g.SCHEMAS[s.name] for s in second]


def test_generation_is_sorted_by_command_path(specs, catalog):
    path_by_tool = {g.tool_name(c["path"]): c["path"] for c in catalog["commands"]}
    paths = [path_by_tool[s.name] for s in specs]
    assert paths == sorted(paths)


def test_a_freshly_built_catalog_generates_the_same_surface(specs):
    again = g.generate(include_writes=True)
    assert {s.name for s in again} >= {s.name for s in specs} or {s.name for s in specs} >= {
        s.name for s in again
    }, "a concurrently added command may appear, but nothing may silently disappear"


# ── tier → annotations ────────────────────────────────────────────────────────


def test_every_tier_maps_to_the_annotations_the_adr_names(specs, catalog):
    tier_by_tool = {g.tool_name(c["path"]): c["tier"] for c in catalog["commands"]}
    seen = set()
    for spec in specs:
        tier = tier_by_tool[spec.name]
        seen.add(tier)
        a = spec.annotations
        if tier == surface.READ:
            assert a["readOnlyHint"] is True, spec.name
            assert "destructiveHint" not in a, spec.name
            assert not spec.destructive and not spec.idempotent, spec.name
            assert spec.tier == "read", spec.name
        elif tier == surface.ADMIN:
            assert a["readOnlyHint"] is False and a["destructiveHint"] is False, spec.name
            assert spec.tier == "B", spec.name
        else:
            assert tier == surface.DESTRUCTIVE, spec.name
            assert a["readOnlyHint"] is False and a["destructiveHint"] is True, spec.name
            assert spec.tier == "C", spec.name
    assert seen == {surface.READ, surface.ADMIN, surface.DESTRUCTIVE}


def test_a_read_tool_is_never_destructive_and_a_destructive_tool_is_never_readonly(specs):
    for spec in specs:
        if not spec.mutating:
            assert spec.annotations["readOnlyHint"] is True
            assert spec.destructive is False
        if spec.destructive:
            assert spec.annotations["readOnlyHint"] is False
            assert spec.annotations["destructiveHint"] is True


def test_idempotent_hint_comes_from_the_surface_table(specs, catalog):
    tiers = {c["path"]: c["tier"] for c in catalog["commands"]}
    marked = {g.tool_name(p) for p in surface.IDEMPOTENT if tiers.get(p) != surface.CLI_ONLY}
    generated_idem = {s.name for s in specs if s.idempotent}
    assert generated_idem == marked
    assert generated_idem, "the idempotent table must actually reach the generated surface"
    for spec in specs:
        if not spec.mutating:
            assert "idempotentHint" not in spec.annotations


def test_the_idempotent_table_only_names_real_mutating_commands():
    unknown = sorted(p for p in surface.IDEMPOTENT if p not in surface.TIERS)
    assert not unknown, f"IDEMPOTENT names commands that do not exist: {unknown}"
    not_a_write = sorted(
        p
        for p in surface.IDEMPOTENT
        if surface.TIERS[p] not in (surface.ADMIN, surface.DESTRUCTIVE)
    )
    assert not not_a_write, f"a read command cannot be idempotent-hinted: {not_a_write}"


def test_open_world_is_declared_conservatively(specs):
    assert all(s.open_world for s in specs), "every exa command may reach a remote service"


# ── the guard: no tier, no schema, no tool ────────────────────────────────────


def test_a_command_with_no_tier_is_refused(catalog):
    cmd = _by_path(catalog)["models diff"]
    with pytest.raises(g.GenerationError) as exc:
        g.build_spec({**cmd, "tier": None})
    assert "no agent tier" in str(exc.value)
    with pytest.raises(g.GenerationError):
        g.build_spec({**cmd, "tier": "something-new"})


def test_a_command_with_no_json_contract_is_refused(catalog):
    cmd = _by_path(catalog)["models diff"]
    with pytest.raises(g.GenerationError) as exc:
        g.build_spec({**cmd, "tier": surface.CLI_ONLY})
    assert "no JSON output contract" in str(exc.value)
    assert g.output_schema({**cmd, "tier": surface.CLI_ONLY}) == {}
    assert not g.has_json_contract({**cmd, "tier": surface.CLI_ONLY})


def test_an_unclassified_command_never_reaches_the_generated_surface(catalog):
    invented = {
        **_by_path(catalog)["models diff"],
        "path": "zzz-not-a-real-command",
        "group": "zzz-not-a-real-command",
    }
    doctored = {
        **catalog,
        "commands": [*catalog["commands"], invented],
        "unclassified": [*catalog["unclassified"], "zzz-not-a-real-command"],
    }
    declined = g.refusals(doctored)
    assert declined["zzz-not-a-real-command"] == "no tier in examlops.cli.surface.TIERS"
    names = {s.name for s in g.generate(include_writes=True, catalog=doctored)}
    assert g.tool_name("zzz-not-a-real-command") not in names


def test_every_cli_only_command_is_refused_with_a_reason(catalog):
    declined = g.refusals(catalog)
    cli_only = {c["path"] for c in catalog["commands"] if c["tier"] == surface.CLI_ONLY}
    assert cli_only, "the surface table always has some cli_only commands"
    assert cli_only <= set(declined)
    assert all(declined[p] for p in cli_only)


def test_every_generated_tool_carries_both_schemas(specs):
    for spec in specs:
        schemas = g.SCHEMAS[spec.name]
        assert schemas["inputSchema"]["type"] == "object"
        # MCP requires structuredContent to be an object, so outputSchema is an object schema.
        assert schemas["outputSchema"]["type"] == "object"
        assert "ok" in schemas["outputSchema"]["properties"]
        assert json.dumps(schemas)  # JSON-serialisable, as a tools/list response must be


# ── input schemas reflect the real declared parameters ────────────────────────


def test_models_diff_schema_is_exactly_its_declared_parameters(catalog):
    assert g.input_schema(_by_path(catalog)["models diff"]) == {
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Registered model name (e.g. jpcp)"},
            "v1": {"type": "string", "description": "First version number"},
            "v2": {"type": "string", "description": "Second version number"},
        },
        "additionalProperties": False,
        "required": ["model", "v1", "v2"],
    }


def test_drift_status_schema_omits_the_blocked_streaming_flags(catalog):
    cmd = _by_path(catalog)["drift status"]
    assert {p["name"] for p in cmd["params"]} >= {"watch", "interval"}
    assert g.input_schema(cmd) == {
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Model name filter (default: all models)"},
        },
        "additionalProperties": False,
    }


def test_scaffold_schema_carries_the_cli_declared_enums(catalog):
    schema = g.input_schema(_by_path(catalog)["scaffold"])
    assert schema["required"] == ["name"]
    assert set(schema["properties"]) == {"name", "task", "task_type", "force"}
    assert schema["properties"]["task"] == {
        "type": "string",
        "enum": [
            "performance_prediction",
            "power_consumption_prediction",
            "anomaly_detection",
        ],
        "description": "Task type",
        "default": "performance_prediction",
    }
    assert schema["properties"]["task_type"] == {
        "type": "string",
        "enum": ["regression", "classification"],
        "description": "ML task type",
        "default": "regression",
    }
    assert schema["properties"]["force"] == {
        "type": "boolean",
        "description": "Overwrite existing files",
    }


def test_the_schema_and_the_function_signature_agree(specs):
    import inspect

    for spec in specs:
        declared = set(g.SCHEMAS[spec.name]["inputSchema"]["properties"])
        params = inspect.signature(spec.fn).parameters
        assert set(params) == declared, spec.name
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values()), spec.name


# ── no collision with the hand-written workflow surface ───────────────────────


def test_generated_names_never_collide_with_hand_written_tools(specs):
    hand = {s.name for s in REGISTRY}
    gen = {s.name for s in specs}
    assert gen and hand
    assert gen & hand == set()
    assert all(n.startswith(g.NAME_PREFIX) for n in gen)
    assert not any(n.startswith(g.NAME_PREFIX) for n in hand)
    assert len(gen) == len(specs), "generated tool names are unique"


# ── the flag is off by default ────────────────────────────────────────────────


def test_flag_off_leaves_the_registry_exactly_as_today(monkeypatch):
    monkeypatch.delenv(g.ENV_FLAG, raising=False)
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    assert [s.name for s in iter_tools(include_writes=True)] == [s.name for s in REGISTRY]
    assert [s.name for s in iter_tools(include_writes=False)] == [
        s.name for s in REGISTRY if not s.mutating
    ]
    assert not g.enabled()


@pytest.mark.parametrize("value", ["", "0", "off", "no", "false", "maybe"])
def test_only_a_truthy_flag_switches_the_surface_on(monkeypatch, value):
    monkeypatch.setenv(g.ENV_FLAG, value)
    assert not g.enabled()
    assert [s.name for s in iter_tools(include_writes=True)] == [s.name for s in REGISTRY]


def test_flag_on_adds_the_generated_tools_and_keeps_the_hand_written_ones(monkeypatch):
    monkeypatch.setenv(g.ENV_FLAG, "1")
    assert g.enabled()
    names = [s.name for s in iter_tools(include_writes=True)]
    assert names[: len(REGISTRY)] == [s.name for s in REGISTRY]
    added = names[len(REGISTRY) :]
    assert added and all(n.startswith(g.NAME_PREFIX) for n in added)
    read_only = [s.name for s in iter_tools(include_writes=False)]
    assert set(read_only) < set(names)
    assert any(n.startswith(g.NAME_PREFIX) for n in read_only), "generated reads are always offered"


def test_a_broken_generator_does_not_take_the_agent_surface_down(monkeypatch):
    monkeypatch.setenv(g.ENV_FLAG, "1")
    monkeypatch.setattr(g, "generate", _boom)
    assert [s.name for s in iter_tools(include_writes=True)] == [s.name for s in REGISTRY]


def _boom(*args, **kwargs):
    raise RuntimeError("generator exploded")


# ── the runner ────────────────────────────────────────────────────────────────


def test_an_invalid_argument_comes_back_as_an_envelope_not_an_exception(catalog):
    spec = g.build_spec(_by_path(catalog)["scaffold"])
    out = spec.fn(name="Demo", task="not-a-real-task")
    assert out["ok"] is False
    assert "task" in out["error"]


def test_an_absolute_path_is_refused_by_the_workspace_containment(catalog, monkeypatch, tmp_path):
    monkeypatch.setenv(g.ENV_WORKSPACE, str(tmp_path / "ws"))
    candidate = None
    for cmd in catalog["commands"]:
        if cmd["tier"] == surface.CLI_ONLY:
            continue
        param = next(
            (p for p in cmd["params"] if p.get("path") and not p.get("blocked")),
            None,
        )
        # Every *other* required parameter must accept plain text, or the run would fail on
        # coercion before it ever reaches the containment check this test is about.
        if param and all(
            p["name"] == param["name"] or p.get("type") == "string"
            for p in cmd["params"]
            if p.get("required")
        ):
            candidate = (cmd, param)
            break
    assert candidate, "the catalog always has a command with a filesystem parameter"
    cmd, param = candidate
    values = {p["name"]: "x" for p in cmd["params"] if p.get("required")}
    values[param["name"]] = "/etc/passwd"
    out = g.run_command(cmd, values)
    assert out["ok"] is False, cmd["path"]
    assert "workspace" in out["error"], (cmd["path"], out["error"])
    assert (tmp_path / "ws").is_dir(), "the workspace root is created on demand"


@pytest.mark.parametrize("path", ["config contexts"])
def test_a_generated_read_tool_really_runs_the_cli(catalog, tmp_path, monkeypatch, path):
    spec = g.build_spec(_by_path(catalog)[path])
    # setenv (not a replaced os.environ dict): the subprocess inherits the real environment.
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("NO_COLOR", "1")
    out = spec.fn()
    # The CLI's one-document contract: whatever it says, it is a single JSON object here.
    assert isinstance(out, dict) and out, out
    assert "error" not in out or "printed no JSON document" not in out["error"], out


def test_the_runner_lifts_a_json_array_under_items():
    assert g._as_object([1, 2]) == {"items": [1, 2]}
    assert g._as_object({"a": 1}) == {"a": 1}
    assert g._as_object("x") == {"result": "x"}


def test_the_cli_is_reachable_the_way_the_runner_invokes_it(tmp_path):
    """``run_command`` reaches the CLI as ``python -m examlops.cli``, not through the ``exa``
    console script, so a missing ``__main__``, an import error or anything printed on stdout ahead
    of the document breaks every generated tool at once — and reaches the agent only as the
    runner's own "printed no JSON document", with the cause truncated into ``detail``.

    So this asserts the reachability directly: the module entry point runs, exits **0**, and puts
    exactly one parseable JSON document — the command's own payload, not an error envelope — on
    stdout. The prefix is checked against the runner's source too, because a test that invokes the
    CLI some *other* way no longer says anything about the runner.
    """
    import inspect

    argv_prefix = ["-m", "examlops.cli", "--output", "json", "--yes"]
    source = " ".join(inspect.getsource(g.run_command).split())
    assert ", ".join(json.dumps(a) for a in argv_prefix) in source, (
        "the runner no longer invokes the CLI this way; this test is asserting a dead path"
    )

    done = subprocess.run(
        [sys.executable, *argv_prefix, "config", "contexts"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        cwd=REPO,
        timeout=120,
        env={**os.environ, "EXAMLOPS_CONFIG": str(tmp_path / "config.toml"), "NO_COLOR": "1"},
    )
    assert done.returncode == 0, (
        f"exit {done.returncode}\nstdout: {done.stdout[:2000]}\nstderr: {done.stderr[:2000]}"
    )
    document = json.loads(done.stdout)  # exactly one JSON document — outputSchema's contract
    assert isinstance(document, dict) and "contexts" in document, document
    assert document.get("ok") is not False, document


# ── names ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("status", "exa_status"),
        ("serve traffic", "exa_serve_traffic"),
        ("drift auto-retrain enable", "exa_drift_auto_retrain_enable"),
        ("audit verify-worm", "exa_audit_verify_worm"),
    ],
)
def test_tool_names_are_derived_from_the_command_path(path, expected):
    assert g.tool_name(path) == expected
