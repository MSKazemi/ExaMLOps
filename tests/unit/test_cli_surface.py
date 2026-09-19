"""The `exa` command surface the dashboard's CLI Console runs (ADR 0119).

Two jobs. First, a **completeness guard**: every leaf command in the live CLI tree has a tier, so
a command added to the CLI cannot silently be missing from the dashboard, and a new mutation
cannot silently default to "a viewer may run it". Second, the **argv builder** is the only thing
between a browser form and a subprocess — every rule it enforces (no unknown params, no blocked
flags, values glued to their option, positionals after ``--``, paths contained in the workspace,
escalation to admin) is pinned here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from examlops.cli import surface as s


@pytest.fixture(scope="module")
def catalog() -> dict:
    return s.build_catalog()


@pytest.fixture(scope="module")
def by_path(catalog) -> dict[str, dict]:
    return {c["path"]: c for c in catalog["commands"]}


# ── completeness: the dashboard can reach every command ──────────────────────────────────


def test_every_live_command_has_a_tier():
    live = set(s.leaf_paths())
    missing = sorted(live - set(s.TIERS))
    assert not missing, (
        "these `exa` commands have no tier in examlops/cli/surface.py TIERS, so the dashboard "
        f"CLI Console would treat them as unclassified: {missing}. Decide read/admin/"
        "destructive/cli_only by what the command does, not what its verb suggests."
    )


def test_no_tier_names_a_command_that_no_longer_exists():
    stale = sorted(set(s.TIERS) - set(s.leaf_paths()))
    assert not stale, f"TIERS names commands the CLI no longer has: {stale}"


def test_catalog_has_no_unclassified_commands(catalog):
    assert catalog["unclassified"] == []
    assert catalog["total"] == len(s.leaf_paths())
    assert sum(catalog["tiers"].values()) == catalog["total"]


def test_no_table_in_the_surface_module_repeats_a_key():
    """A repeated key in a dict literal silently keeps the *last* value.

    In ``TIERS`` that means a later line can quietly downgrade a command's tier with nothing
    failing; this has happened in editing, so it is checked rather than trusted.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(s))
    dupes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            dupes += sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"repeated keys in examlops/cli/surface.py: {dupes}"


def test_every_tier_is_a_known_value():
    assert set(s.TIERS.values()) <= {s.READ, s.ADMIN, s.DESTRUCTIVE, s.CLI_ONLY}


def test_cli_only_is_the_exception_and_always_says_why():
    cli_only = {p for p, t in s.TIERS.items() if t == s.CLI_ONLY}
    assert cli_only == set(s.CLI_ONLY_REASONS), (
        "every cli_only command needs a reason (and only those)"
    )
    assert all(len(r) > 40 for r in s.CLI_ONLY_REASONS.values())
    # The console exists to make the CLI's potential reachable: keep "cli_only" rare. 12 since
    # ADR 0120: `auth login` (an interactive device sign-in) and `auth token` (prints the caller's
    # bearer credential) have no safe browser form — the dashboard signs users in its own way.
    # 13 since ADR 0124's first consumers: `autopilot follow` is a long-running event consumer,
    # unrunnable as a request/response command exactly as `mcp serve` is.
    # 14 since ADR 0123 decision 4: `drift consume-telemetry` is the same shape of long-running
    # event consumer as `autopilot follow`, for the serving-plane telemetry event instead.
    assert len(cli_only) <= 14, (
        f"{len(cli_only)} cli_only commands — is each one really unrunnable?"
    )


def test_every_command_appears_in_a_lifecycle_panel(catalog):
    assert {c["panel"] for c in catalog["commands"]} <= set(catalog["panels"])


# ── classification sanity: mutation verbs are never a viewer read ────────────────────────

_MUTATING_VERBS = {
    "create", "set", "add", "delete", "rm", "register", "record", "apply", "ingest", "enable",
    "disable", "start", "stop", "submit", "publish", "snapshot", "sync", "issue", "revoke",
    "promote", "deploy", "reload", "reset", "restore", "rotate", "prune", "approve", "reject",
    "grant", "assign", "use", "label", "rollback", "run", "trigger", "import", "upsert",
    "reindex", "materialize", "sign", "quantize", "fit", "launch", "resume", "connect", "burst",
    "pack", "baseline", "retrain", "scaffold", "finetune", "init", "archive",
}  # fmt: skip
# Reviewed exceptions: pure computations whose name happens to be a verb.
_READ_VERB_EXCEPTIONS = {"hpc gpu-share pack"}

_ALWAYS_DESTRUCTIVE_VERBS = {
    "delete", "rm", "prune", "restore", "restore-bundle", "archive", "rotate", "rewrap",
    "retention-prune", "reset", "quarantine", "interrupt", "traffic",
}  # fmt: skip


def test_no_mutating_verb_is_classified_read():
    offenders = [
        p
        for p, t in s.TIERS.items()
        if t == s.READ and p.split(" ")[-1] in _MUTATING_VERBS and p not in _READ_VERB_EXCEPTIONS
    ]
    assert not offenders, f"read tier on a mutating verb: {offenders}"


def test_irreversible_verbs_are_destructive():
    offenders = [
        p
        for p, t in s.TIERS.items()
        if p.split(" ")[-1] in _ALWAYS_DESTRUCTIVE_VERBS and t not in (s.DESTRUCTIVE, s.CLI_ONLY)
    ]
    assert not offenders, (
        f"these erase or overwrite state and need a typed confirmation: {offenders}"
    )


def test_a_command_with_a_dry_run_is_never_a_viewer_read(by_path):
    # A --dry-run flag exists because the real run changes something.
    offenders = [
        p
        for p, c in by_path.items()
        if c["tier"] == s.READ and any(q["name"] == "dry_run" for q in c["params"])
    ]
    assert not offenders, offenders


def test_governance_surfaces_stay_admin():
    # The dashboard already hides these consoles from viewers; the console must not reopen them.
    for group in ("audit", "approvals", "secrets", "backup", "fairness"):
        tiers = {t for p, t in s.TIERS.items() if p.split(" ")[0] == group}
        assert s.READ not in tiers, f"`exa {group}` must not be runnable by viewers"


# ── the side tables point at real commands and params ───────────────────────────────────


def _params(by_path, path) -> set[str]:
    return {p["name"] for p in by_path[path]["params"]}


def test_blocked_and_forced_tables_name_real_params(by_path):
    for path, names in s.BLOCKED_PARAMS.items():
        assert names <= _params(by_path, path), (path, names)
    for path in s.FORCED_ARGS:
        assert path in by_path


def test_param_override_tables_name_real_params(by_path):
    for table in (s._NOT_FS, s._EXTRA_FS, s._PATH_OR_URL, s._SECRET_PARAMS):
        for path, name in table:
            assert name in _params(by_path, path), (path, name)


_FS_HELP = re.compile(r"\b(file|files|path|dir|directory|jsonl|parquet|csv)\b", re.I)


def test_every_parameter_that_says_it_is_a_file_is_contained_or_explicitly_not(by_path):
    """A new `--foo-file` must not reach the subprocess unconstrained.

    Any string parameter whose help text talks about a file, path or directory is either treated
    as a filesystem path (contained in the workspace) or listed in ``_NOT_FS`` with the reason it
    is not one (a secret's path in the store, an HF model id, a directory on another host).
    """
    loose = [
        (path, p["name"])
        for path, c in by_path.items()
        for p in c["params"]
        if p["type"] == "string"
        and _FS_HELP.search(p.get("help", ""))
        and not p.get("path")
        and (path, p["name"]) not in s._NOT_FS
    ]
    assert not loose, f"file-ish params with no containment decision: {loose}"


# ── argv building ────────────────────────────────────────────────────────────────────────


def _argv(by_path, path, values, tmp_path):
    return s.build_argv(by_path[path], values, workspace=tmp_path)


def test_positionals_follow_a_double_dash_so_a_dash_value_stays_a_value(by_path, tmp_path):
    inv = _argv(by_path, "drift status", {"model": "-JPCP"}, tmp_path)
    assert inv.argv == ["drift", "status", "--", "-JPCP"]
    assert inv.tier == s.READ


def test_option_values_are_glued_to_their_option(by_path, tmp_path):
    inv = _argv(by_path, "project create", {"name": "p1", "description": "--yes"}, tmp_path)
    assert "--description=--yes" in inv.argv
    assert inv.argv[-2:] == ["--", "p1"]


def test_variadic_argument_takes_a_list(by_path, tmp_path):
    inv = _argv(by_path, "ask", {"question": ["how", "many", "models"]}, tmp_path)
    assert inv.argv == ["ask", "--", "how", "many", "models"]


def test_flags_render_only_when_they_differ_from_the_default(by_path, tmp_path):
    base = _argv(by_path, "pipeline run", {"dummy": False}, tmp_path)
    assert "--dummy" not in base.argv
    on = _argv(by_path, "pipeline run", {"dummy": True}, tmp_path)
    assert "--dummy" in on.argv


def test_secondary_flag_spelling_is_used_to_turn_a_default_off(by_path, tmp_path):
    # `cards model` saves by default; turning that off needs the `--no-save` spelling.
    inv = _argv(by_path, "cards model", {"model": "jpcp", "save": False}, tmp_path)
    assert "--no-save" in inv.argv
    assert "--save" not in inv.argv


def test_missing_required_parameter_is_refused(by_path, tmp_path):
    with pytest.raises(s.SurfaceError, match="missing required"):
        _argv(by_path, "project show", {}, tmp_path)


def test_unknown_parameter_is_refused(by_path, tmp_path):
    with pytest.raises(s.SurfaceError, match="unknown parameter"):
        _argv(by_path, "project list", {"shell": "rm -rf /"}, tmp_path)


def test_choice_values_are_checked(by_path, tmp_path):
    with pytest.raises(s.SurfaceError, match="must be one of"):
        _argv(by_path, "pipeline run", {"backend": "ftp"}, tmp_path)
    assert "--backend=minio" in _argv(by_path, "pipeline run", {"backend": "minio"}, tmp_path).argv


def test_numbers_are_checked(by_path, tmp_path):
    with pytest.raises(s.SurfaceError, match="integer"):
        _argv(by_path, "hpc jobs", {"limit": "ten"}, tmp_path)
    with pytest.raises(s.SurfaceError, match="integer"):
        _argv(by_path, "hpc jobs", {"limit": True}, tmp_path)
    assert "--limit=10" in _argv(by_path, "hpc jobs", {"limit": "10"}, tmp_path).argv


def test_nul_and_oversized_values_are_refused(by_path, tmp_path):
    with pytest.raises(s.SurfaceError):
        _argv(by_path, "project show", {"name": "a\x00b"}, tmp_path)
    with pytest.raises(s.SurfaceError):
        _argv(by_path, "project show", {"name": "x" * 20_000}, tmp_path)


def test_blocked_flags_are_refused(by_path, tmp_path):
    with pytest.raises(s.SurfaceError, match="not available"):
        _argv(by_path, "status", {"watch": True}, tmp_path)
    with pytest.raises(s.SurfaceError, match="not available"):
        _argv(by_path, "secrets get", {"path": "control-plane/token", "reveal": True}, tmp_path)


def test_forced_args_keep_runs_finite(by_path, tmp_path):
    assert "--no-follow" in _argv(by_path, "stack logs", {}, tmp_path).argv
    assert "--once" in _argv(by_path, "backup schedule", {}, tmp_path).argv


def test_cli_only_commands_are_refused_with_their_reason(by_path, tmp_path):
    with pytest.raises(s.SurfaceError, match="Services console"):
        _argv(by_path, "stack down", {}, tmp_path)


# ── a run never reaches a prompt ─────────────────────────────────────────────────────────


def test_a_commands_own_yes_flag_is_implied_by_the_consoles_confirmation(by_path, tmp_path):
    # `exa backup restore` calls typer.confirm unless given its own --yes; on /dev/null that
    # aborts. The console's confirmation stands in for it, so the flag is always passed and
    # never shown as a checkbox.
    with_yes = [p for p, c in by_path.items() if any(q["name"] == "yes" for q in c["params"])]
    assert "backup restore" in with_yes
    for path in with_yes:
        spec = next(q for q in by_path[path]["params"] if q["name"] == "yes")
        assert spec.get("implied"), path
        required = {
            q["name"]: "x"
            for q in by_path[path]["params"]
            if q.get("required") and not q.get("flag")
        }
        inv = _argv(by_path, path, required, tmp_path)
        assert "--yes" in inv.argv and "--yes" in inv.display, path


def test_values_a_command_would_prompt_for_are_required_in_the_console(by_path, tmp_path):
    with pytest.raises(s.SurfaceError, match="missing required"):
        _argv(by_path, "config set", {"key": "token"}, tmp_path)
    with pytest.raises(s.SurfaceError, match="missing required"):
        _argv(by_path, "models rollback run", {"model": "jpcp"}, tmp_path)
    assert _argv(by_path, "models rollback run", {"model": "jpcp", "version": "3"}, tmp_path)


_PROMPT_CALL = re.compile(r"\b(typer|click)\.(prompt|confirm)\(|\binput\(")


def test_no_runnable_command_can_block_on_a_prompt(by_path):
    """stdin is /dev/null in the console: a prompt aborts the run instead of asking.

    Any command whose own code prompts must be `cli_only`, carry an implied `--yes`, or have the
    prompted value required in the console (`surface._CONSOLE_REQUIRED`). `_output.confirm` is
    fine — the console always passes the global `--yes`.
    """
    import inspect

    root = s.live_tree()

    def leaf(path):
        node = root
        for word in path.split(" "):
            node = node.commands[word]
        return node

    offenders = []
    for path, c in by_path.items():
        if c["tier"] == s.CLI_ONLY:
            continue
        src = inspect.getsource(leaf(path).callback)
        if not _PROMPT_CALL.search(src):
            continue
        covered = any(q.get("implied") for q in c["params"]) or any(
            key[0] == path for key in s._CONSOLE_REQUIRED
        )
        if not covered:
            offenders.append(path)
    assert not offenders, f"these commands can prompt and would abort in the console: {offenders}"


# ── filesystem containment ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../x", "~/x", "~root", "C:\\x"])
def test_paths_outside_the_workspace_are_refused(by_path, tmp_path, bad):
    with pytest.raises(s.SurfaceError, match="workspace"):
        _argv(by_path, "audit export", {"out": bad}, tmp_path)


def test_a_symlink_that_leaves_the_workspace_is_refused(by_path, tmp_path):
    (tmp_path / "escape").symlink_to("/etc")
    with pytest.raises(s.SurfaceError, match="escapes"):
        _argv(by_path, "secrets scan", {"target": "escape/passwd"}, tmp_path)


def test_paths_inside_the_workspace_pass_and_are_reported(by_path, tmp_path):
    inv = _argv(by_path, "audit export", {"out": "exports/./audit.json"}, tmp_path)
    # The command gets the absolute workspace path, so it can run from the repo root; people
    # see (and the audit records) the workspace-relative path they typed.
    assert f"--out={tmp_path.resolve()}/exports/audit.json" in inv.argv
    assert "--out=exports/audit.json" in inv.display
    assert inv.paths == ["exports/audit.json"]
    assert inv.redacted["out"] == "exports/audit.json"


def test_a_secret_path_is_not_treated_as_a_file(by_path, tmp_path):
    inv = _argv(by_path, "secrets set", {"path": "/control-plane/token", "value": "v"}, tmp_path)
    assert inv.argv == ["secrets", "set", "--", "/control-plane/token", "v"]
    assert inv.paths == []


def test_path_or_url_lets_a_url_through_but_contains_a_path(by_path, tmp_path):
    ok = _argv(
        by_path,
        "serve llm chat",
        {"model": "m", "message": "hi", "image": ["https://x/y.png"]},
        tmp_path,
    )
    assert "--image=https://x/y.png" in ok.argv
    with pytest.raises(s.SurfaceError):
        _argv(
            by_path,
            "serve llm chat",
            {"model": "m", "message": "hi", "image": ["/etc/x.png"]},
            tmp_path,
        )


# ── escalation + redaction ───────────────────────────────────────────────────────────────


def test_a_persisting_flag_makes_a_read_admin(by_path, tmp_path):
    assert _argv(by_path, "models cost", {"model": "jpcp"}, tmp_path).tier == s.READ
    assert (
        _argv(by_path, "models cost", {"model": "jpcp", "record": True}, tmp_path).tier == s.ADMIN
    )


def test_a_filesystem_path_makes_a_read_admin(by_path, tmp_path):
    assert _argv(by_path, "docs", {}, tmp_path).tier == s.READ
    assert _argv(by_path, "docs", {"out": "cli.md"}, tmp_path).tier == s.ADMIN


def test_a_network_target_makes_a_read_admin(by_path, tmp_path):
    assert _argv(by_path, "mcp agent-card", {"base_url": "http://x"}, tmp_path).tier == s.ADMIN


def test_acting_on_an_agent_proposal_makes_ask_admin(by_path, tmp_path):
    assert _argv(by_path, "ask", {"question": ["hi"]}, tmp_path).tier == s.READ
    assert _argv(by_path, "ask", {"approve": "a1"}, tmp_path).tier == s.ADMIN


def test_secret_values_are_redacted_in_the_record(by_path, tmp_path):
    inv = _argv(by_path, "secrets set", {"path": "cp/token", "value": "hunter2"}, tmp_path)
    assert inv.redacted["value"] == "***"
    assert "hunter2" not in json.dumps(inv.redacted)
    conn = _argv(by_path, "connection create", {"name": "c", "secret_value": "pw"}, tmp_path)
    assert conn.redacted["secret_value"] == "***"
    # The display argv is what the console shows, logs and audits: the value never appears.
    assert "hunter2" not in " ".join(inv.display)
    assert inv.display == ["secrets", "set", "--", "cp/token", "***"]
    assert "--secret-value=***" in conn.display
    assert "pw" not in conn.display


def test_context_names_are_validated():
    assert s.valid_context("staging-1") == "staging-1"
    for bad in ("", "a b", "x;rm", "../x", "a" * 65):
        with pytest.raises(s.SurfaceError):
            s.valid_context(bad)


# ── end to end: the argv really runs ─────────────────────────────────────────────────────


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "examlops.cli", "--output", "json", "--yes", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "NO_COLOR": "1"},
        timeout=120,
    )


def test_built_argv_runs_against_the_real_cli(by_path, tmp_path):
    create = _argv(
        by_path, "project create", {"name": "surface-e2e", "description": "-x"}, tmp_path
    )
    done = _run(create.argv, tmp_path)
    assert done.returncode == 0, done.stderr
    show = _argv(by_path, "project show", {"name": "surface-e2e"}, tmp_path)
    shown = _run(show.argv, tmp_path)
    assert shown.returncode == 0, shown.stderr
    body = json.loads(shown.stdout)
    assert body["name"] == "surface-e2e"
    assert body["description"] == "-x"


def test_the_module_prints_the_catalog_as_json(tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "examlops.cli.surface"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert data["total"] == len(s.TIERS)
