"""Resources: the CLI's verbs composed into manageable objects for the dashboard (ADR 0119).

The Resource Manager renders every resource from `examlops.cli.resources` — a table from the list
command, a create form, and row actions pre-filled from the row. If a binding points at the wrong
parameter the dashboard would run a real command on the wrong object, so every binding is checked
against the live CLI tree here, and the important ones are exercised end to end against the real
CLI on a private datastore.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from examlops.cli import resources as res
from examlops.cli import surface as s


@pytest.fixture(scope="module")
def catalog() -> dict:
    return s.build_catalog()


@pytest.fixture(scope="module")
def by_path(catalog) -> dict[str, dict]:
    return {c["path"]: c for c in catalog["commands"]}


@pytest.fixture(scope="module")
def resources(catalog) -> dict[str, dict]:
    return {r["id"]: r for r in catalog["resources"]}


# ── the spec names real things ────────────────────────────────────────────────────────────


def test_every_resource_command_exists(by_path):
    missing = [(r.id, c) for r in res.RESOURCES for c in r.commands() if c not in by_path]
    assert not missing, f"resources name commands the CLI does not have: {missing}"


def test_resource_ids_are_unique():
    ids = [r.id for r in res.RESOURCES]
    assert len(ids) == len(set(ids))


def test_every_binding_names_a_real_parameter(resources, by_path):
    bad = [
        (rid, cmd, param)
        for rid, r in resources.items()
        for cmd, binding in r["bindings"].items()
        for param in binding
        if param not in {p["name"] for p in by_path[cmd]["params"]}
    ]
    assert not bad, bad


def test_every_row_action_receives_an_identifying_row_field(resources):
    # Usually the key itself; sometimes another part of a multi-field identity (an approval is
    # deleted by its `id`, `slo burn` takes the SLO's `model`). Never nothing.
    unbound = [
        (rid, cmd)
        for rid, r in resources.items()
        for cmd, binding in r["bindings"].items()
        if not binding
    ]
    assert not unbound, f"these row actions would not know which item to act on: {unbound}"


def test_delete_actions_change_state(resources):
    loose = [
        r["id"] for r in resources.values() if r["delete"] and r["tiers"][r["delete"]] == s.READ
    ]
    assert not loose


def test_every_list_command_is_a_resource_or_says_why_not(by_path):
    lists = {p for p in by_path if p.split(" ")[-1] in {"list", "cost-list", "traffic-list"}}
    placed = {r.list for r in res.RESOURCES} | set(res.NOT_RESOURCES)
    unplaced = sorted(lists - placed)
    assert not unplaced, (
        f"list commands that are neither a resource nor in NOT_RESOURCES: {unplaced}. Add a "
        "Resource in examlops/cli/resources.py, or say why it is not one."
    )
    assert set(res.NOT_RESOURCES) <= set(by_path)


def test_multi_field_identities_bind_each_part(resources):
    # An SLO is (model, name): `slo burn` takes only the model — not the SLO's name.
    assert resources["slo"]["bindings"]["slo burn"] == {"model": "model"}
    # A dataset revision is (dataset, revision_id).
    assert resources["dataset-revision"]["bindings"]["data validate"] == {
        "dataset": "dataset",
        "revision": "revision_id",
    }


def test_coverage_is_reported(catalog):
    cov = catalog["resource_coverage"]
    assert cov["resources"] == len(res.RESOURCES)
    assert 0 < cov["commands"] <= cov["runnable"]


# ── row handling ──────────────────────────────────────────────────────────────────────────


def test_rows_of_normalises_every_list_shape():
    r = {"key": "name", "rows_key": None}
    assert res.rows_of([{"name": "a"}], r) == [{"name": "a"}]
    assert res.rows_of(["a", "b"], r) == [{"name": "a"}, {"name": "b"}]  # `exa prompt list`
    assert res.rows_of({"path": "/x", "policies": [{"name": "p"}], "error": None}, r) == [
        {"name": "p"}
    ]
    assert res.rows_of(
        {"snapshots": [1], "bundles": [2]}, {"key": "path", "rows_key": "bundles"}
    ) == [{"path": 2}]
    assert res.rows_of({"ok": True, "message": "none"}, r) == []


def test_prefill_never_puts_a_display_string_in_a_number_field():
    command = {
        "path": "finops budget set",
        "params": [
            {"name": "project", "kind": "argument", "type": "string"},
            {"name": "cost", "kind": "option", "type": "float"},
            {"name": "gpu_hours", "kind": "option", "type": "float"},
            {"name": "period", "kind": "option", "type": "choice", "choices": ["month", "year"]},
        ],
    }
    resource = {"bindings": {"finops budget set": {"project": "project"}}}
    row = {"project": "p1", "cost": "0 / —", "gpu_hours": 12.5, "period": "decade"}
    assert res.prefill(resource, command, row) == {"project": "p1", "gpu_hours": 12.5}


def test_prefill_uses_bindings_then_matching_fields(resources, by_path):
    row = {"name": "c1", "project": "p1", "kind": "uri", "config": {"a": 1}}
    values = res.prefill(resources["connection"], by_path["connection delete"], row)
    assert values == {"name": "c1", "project": "p1"}
    model_row = {"Name": "jpcp", "Production": "3"}
    assert res.prefill(resources["model"], by_path["models info"], model_row) == {"model": "jpcp"}


# ── end to end on the real CLI ────────────────────────────────────────────────────────────


def _hermetic_env() -> dict[str, str]:
    # Real CLI, no real services: every URL points at a port nothing serves.
    from examlops.cli import _config

    env = {**os.environ, "NO_COLOR": "1", "DOCKER_HOST": "unix:///nonexistent/docker.sock"}
    for _field, _key, var, _default, _secret in _config._FIELDS:
        env[var] = "http://127.0.0.1:9" if var.endswith(("_URL", "_URI")) else ""
    return env


def _exa(*argv: str, cwd: Path) -> dict | list:
    done = subprocess.run(
        [sys.executable, "-m", "examlops.cli", "--output", "json", "--yes", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=_hermetic_env(),
        timeout=120,
    )
    assert done.returncode == 0, (argv, done.stdout, done.stderr)
    return json.loads(done.stdout) if done.stdout.strip() else {}


def _run(by_path, path, values, cwd):
    return _exa(*s.build_argv(by_path[path], values, workspace=cwd).argv, cwd=cwd)


@pytest.mark.parametrize(
    ("rid", "create_values", "row_action", "typed"),
    [
        ("project", {"name": "res-p1", "description": "d"}, "project show", {}),
        ("namespace", {"name": "res-ns"}, "namespace info", {}),
        # The row is just a name; the operator types which version to show.
        ("prompt", {"name": "res-pr", "template": "hi {x}"}, "prompt show", {"version": "1"}),
        ("slo", {"model": "JPCP", "name": "res-avail", "target": "0.99"}, "slo status", {}),
        ("virtual-key", {"tenant": "res-t"}, None, {}),
        ("ab-test", {"model": "RESM"}, "serve ab analyze", {}),
        (
            "budget",
            {"project": "res-b", "gpu_hours": "10"},
            "finops budget set",
            {"gpu_hours": "20"},
        ),
        ("auto-retrain", {"model": "RESM", "dataset": "ds"}, "drift auto-retrain disable", {}),
        (
            "ai-system",
            {"model": "RESM", "risk_tier": "high", "purpose": "scoring"},
            "compliance declare",
            {"state": "draft"},
        ),
        ("shadow", {"model": "RESM"}, "serve shadow log", {}),
    ],
)
def test_create_then_list_then_act_on_the_listed_row(
    tmp_path, monkeypatch, resources, by_path, rid, create_values, row_action, typed
):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    r = resources[rid]
    _run(by_path, r["create"], create_values, tmp_path)
    rows = res.rows_of(_run(by_path, r["list"], {}, tmp_path), r)
    assert rows, f"{r['list']} listed nothing after {r['create']}"
    row = rows[-1]
    assert res.row_value(row, r["key"]) is not None, f"rows of {r['list']} carry no {r['key']!r}"
    for path in [row_action, r["delete"]]:
        if not path:
            continue
        values = {**res.prefill(r, by_path[path], row), **(typed if path == row_action else {})}
        missing = [
            p["name"]
            for p in by_path[path]["params"]
            if p.get("required") and p["name"] not in values
        ]
        assert not missing, (path, missing)
        _run(by_path, path, values, tmp_path)


def test_deleting_the_listed_row_removes_it(tmp_path, monkeypatch, resources, by_path):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    r = resources["project"]
    _run(by_path, "project create", {"name": "res-gone"}, tmp_path)
    row = next(
        x for x in res.rows_of(_run(by_path, r["list"], {}, tmp_path), r) if x["name"] == "res-gone"
    )
    _run(by_path, r["delete"], res.prefill(r, by_path[r["delete"]], row), tmp_path)
    names = [x["name"] for x in res.rows_of(_run(by_path, r["list"], {}, tmp_path), r)]
    assert "res-gone" not in names
