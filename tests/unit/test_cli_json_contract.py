"""`exa --json <command>` prints exactly one JSON value on stdout — for every read command.

Scripts, the MCP server and the dashboard (CLI Console tables, the Resource Manager, the config
page) parse `--json` output. A sweep on an empty datastore found 13 of 103 read commands broke
that contract three ways: printing *nothing* when there was no data (their only line was an
`info()` message, which JSON mode suppresses), printing *two* documents (an `ok()` message and
then the record), or letting an external tool (`docker compose`, `pytest`, the pipeline
generator) write straight to stdout.

Hermetic by construction: every service URL points at a port nothing listens on, Docker at a
socket that does not exist, and the datastore and CLI config are per-test. Each command therefore
takes its real error or empty path — which must still be one JSON value.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from examlops.cli import _config
from examlops.cli import surface as s

REPO = Path(__file__).resolve().parents[2]
DEAD = "http://127.0.0.1:9"


def _hermetic_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EXAMLOPS_CONTEXT")}
    for _field, _key, var, _default, _secret in _config._FIELDS:
        env[var] = DEAD if var.endswith(("_URL", "_URI")) else ""
    env.update(
        {
            "PLATFORM_DB": str(tmp_path / "platform.db"),
            "EXAMLOPS_CONFIG": str(tmp_path / "config.toml"),
            "DOCKER_HOST": "unix:///nonexistent/docker.sock",
            "NO_COLOR": "1",
        }
    )
    return env


def _one_json_value(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def test_every_read_command_prints_exactly_one_json_value(tmp_path):
    catalog = s.build_catalog()
    commands = [
        c
        for c in catalog["commands"]
        if c["tier"] == s.READ and not any(p.get("required") for p in c["params"])
    ]
    env = _hermetic_env(tmp_path)

    def run(cmd: dict) -> tuple[str, str]:
        argv = s.build_argv(cmd, {}, workspace=tmp_path).argv
        try:
            done = subprocess.run(
                [sys.executable, "-m", "examlops.cli", "--output", "json", "--yes", *argv],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=env,
                cwd=REPO,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return cmd["path"], "timed out"
        out = done.stdout.strip()
        if not out:
            return cmd["path"], f"printed nothing (exit {done.returncode})"
        if not _one_json_value(out):
            return cmd["path"], f"not one JSON value: {out[:120]!r}"
        return cmd["path"], ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, commands))
    broken = [f"exa {path}: {why}" for path, why in results if why]
    assert not broken, "--json broke its contract:\n  " + "\n  ".join(broken)


def test_an_empty_result_still_says_so_in_json(tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "examlops.cli", "--json", "finops", "budget", "status"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=_hermetic_env(tmp_path),
        cwd=REPO,
        timeout=120,
    )
    payload = json.loads(done.stdout)
    assert payload["ok"] is True
    assert "budget" in payload["message"].lower()  # the suppressed info() line is not lost


@pytest.mark.parametrize("fmt", ["yaml", "csv"])
def test_the_fallback_honours_the_chosen_format(tmp_path, fmt):
    done = subprocess.run(
        [sys.executable, "-m", "examlops.cli", "--output", fmt, "assets", "status"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=_hermetic_env(tmp_path),
        cwd=REPO,
        timeout=120,
    )
    assert done.stdout.strip(), "a structured format must not print nothing"
    assert "{" not in done.stdout  # yaml/csv, not JSON
