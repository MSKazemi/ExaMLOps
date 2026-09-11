"""`exa instance`, `exa upgrade`, `exa modules` end to end through the CLI (ADR 0128)."""

from __future__ import annotations

import json
import os

import yaml
from typer.testing import CliRunner

from examlops.cli.main import app

runner = CliRunner()


def _json(*args: str):
    res = runner.invoke(app, ["--json", *args])
    return res, json.loads(res.stdout)


def _pack(root):
    (root / "models").mkdir(parents=True)
    (root / "pack.toml").write_text('[pack]\nname = "demo"\nrequires_examlops = ">=0.1"\n')
    return root


def test_instance_init_creates_a_working_data_root(tmp_path, monkeypatch):
    from examlops.cli import main as cli_main

    monkeypatch.delenv("PLATFORM_DB")
    # With PLATFORM_DB unset the CLI's start-up init opens the default datastore — in a source
    # checkout, the checkout's own platform.db, which on a dev host is the live stack's. The
    # command under test creates the new root's database itself (asserted below).
    monkeypatch.setattr(cli_main, "_init_platform_db", lambda: None)
    monkeypatch.delenv("EXAMLOPS_SITE_PROFILE")  # let the profile follow the new data root
    root = tmp_path / "data"
    res, out = _json(
        "instance", "init", "--data-dir", str(root), "--pack", str(_pack(tmp_path / "p")),
        "--preset", "standard", "--site-name", "centre-a",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert out["stamp"]["data_format"] >= 1
    assert (root / "usecase" / "pack.toml").is_file()
    if os.getenv("EXAMLOPS_DB_BACKEND", "sqlite") != "postgres":  # on Postgres there is no file
        assert (root / "platform.db").is_file()
    assert 'preset = "standard"' in (root / "site.toml").read_text()


def test_instance_init_needs_a_root(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATA_DIR", raising=False)
    res = runner.invoke(app, ["instance", "init"])
    assert res.exit_code == 1


def test_instance_info_reports_the_three_layers(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))
    res, out = _json("instance", "info")
    assert res.exit_code == 0
    assert set(out) == {"core", "deployment", "data", "modules"}
    assert out["core"]["data_format"] >= 1
    assert out["data"]["root"] == str(tmp_path)
    assert out["modules"]["preset"] == "full"
    assert any(loc["name"] == "platform datastore" for loc in out["data"]["locations"])
    human = runner.invoke(app, ["instance", "info"])
    assert human.exit_code == 0 and "Instance data" in human.output


def test_instance_check_passes_on_a_healthy_install_and_fails_on_bad_data(tmp_path, monkeypatch):
    from examlops.data import get_db, init_db

    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(_pack(tmp_path / "pack")))
    init_db(force=True)  # stamp now: the Postgres test harness truncates every table between tests
    res, out = _json("instance", "check")
    assert res.exit_code == 0, out
    assert out["ok"] is True
    assert any(c["check"] == "use-case pack supports this release" for c in out["checks"])

    with get_db() as conn:
        conn.execute("UPDATE platform_meta SET value='77' WHERE key IN "
                     "('data_format','min_reader_format')")  # fmt: skip
    res, out = _json("instance", "check")
    assert res.exit_code == 1 and out["ok"] is False


def test_upgrade_plan_apply_history(tmp_path):
    res, plan = _json("upgrade", "plan")
    assert res.exit_code == 0 and plan["ready"] is True
    res, applied = _json("--yes", "upgrade", "apply", "--backup-dir", str(tmp_path / "bk"))
    assert res.exit_code == 0 and applied["ok"] is True
    res, rows = _json("upgrade", "history")
    assert res.exit_code == 0 and rows and rows[-1]["kind"] == "create"
    human = runner.invoke(app, ["upgrade", "plan"])
    assert human.exit_code == 0 and "Nothing to migrate" in human.output


def test_modules_enable_disable_render(tmp_path, monkeypatch):
    site = tmp_path / "site.toml"
    monkeypatch.setenv("EXAMLOPS_SITE_PROFILE", str(site))
    res, out = _json("modules", "disable", "genai")
    assert res.exit_code == 0 and out["changed"] == {"genai": False, "llm-serving": False}
    res, rows = _json("modules", "list")
    state = {r["module"]: r["enabled"] for r in rows}
    assert state["genai"] is False and state["core"] is True
    res, shown = _json("modules", "show", "llm-serving")
    assert shown["enabled"] is False and "requires 'genai'" in shown["why"]
    res = runner.invoke(app, ["modules", "disable", "core"])
    assert res.exit_code == 1
    res = runner.invoke(app, ["modules", "enable", "warp"])
    assert res.exit_code == 1

    out_file = tmp_path / "values.yaml"
    res = runner.invoke(app, ["modules", "render", "--target", "helm", "--out", str(out_file)])
    assert res.exit_code == 0
    assert (
        yaml.safe_load(out_file.read_text())["site"]["features"]
        == "preset:full,-genai,-llm-serving"
    )
    res, env = _json("modules", "render")
    assert env["EXAMLOPS_FEATURES"] == "preset:full,-genai,-llm-serving"
    res = runner.invoke(app, ["modules", "render", "--target", "nope"])
    assert res.exit_code == 1

    res = runner.invoke(app, ["--yes", "modules", "reset"])
    assert res.exit_code == 0 and not site.exists()


def test_module_changes_are_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SITE_PROFILE", str(tmp_path / "site.toml"))
    runner.invoke(app, ["modules", "preset", "minimal"])
    from examlops.data import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT source, action FROM audit_events WHERE source = 'exa-modules'"
        ).fetchall()
    assert ("exa-modules", "modules_preset") in [tuple(r) for r in rows]
