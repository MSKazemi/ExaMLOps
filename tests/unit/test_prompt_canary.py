# tests/unit/test_prompt_canary.py
"""BL-109 -- weighted/canary rollout of a prompt version (ADR 0009).

`exa prompt label` moves a label to exactly one version; nothing let an operator stage a prompt
change across a fraction of traffic first, even though ADR 0117/0024 already apply that idea to
*model* versions. This covers the additive `prompt_label_splits` table, `get_prompt`'s weighted
resolution, backward compatibility with the no-split path, and the `exa prompt canary` CLI.
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import prompts  # noqa: E402
from examlops.cli.commands import prompt_cmd  # noqa: E402
from examlops.data.prompts import (  # noqa: E402
    clear_prompt_split,
    create_prompt_version,
    get_prompt_split,
    set_prompt_label,
    set_prompt_split,
    use_backend,
)
from examlops.platform_db import init_db  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_PROMPT_BACKEND", raising=False)
    init_db()
    prompts.clear_cache()
    yield
    prompts.clear_cache()


@pytest.fixture
def two_versions():
    v1 = create_prompt_version("triage", "v1 {x}", variables=["x"], actor="me")
    v2 = create_prompt_version("triage", "v2 {x}", variables=["x"], actor="me")
    set_prompt_label("triage", "prod", v1)
    return v1, v2


def test_no_split_is_byte_identical_to_before(two_versions):
    v1, _v2 = two_versions
    assert get_prompt_split("triage", "prod") == []
    pv = prompts.get_prompt("triage", "prod")
    assert pv.version == v1
    # Cached path still short-circuits exactly as before when no split exists.
    cached = prompts._cache[("triage", "prod")]
    assert cached[0].version == v1


def test_split_draws_versions_in_proportion_to_weight(two_versions):
    v1, v2 = two_versions
    set_prompt_split("triage", "prod", {v1: 0.9, v2: 0.1}, actor="me")
    rng = random.Random(1234)
    draws = [prompts.get_prompt("triage", "prod", rng=rng).version for _ in range(2000)]
    counts = Counter(draws)
    # Statistical, not exact: with n=2000 draws and p=0.1/0.9 a 5% absolute tolerance is generous.
    assert counts[v2] / len(draws) == pytest.approx(0.1, abs=0.05)
    assert counts[v1] / len(draws) == pytest.approx(0.9, abs=0.05)


def test_split_resolves_fresh_every_call_not_cached(two_versions):
    v1, v2 = two_versions
    set_prompt_split("triage", "prod", {v1: 0.5, v2: 0.5}, actor="me")
    # A biased rng that always picks the second candidate (v2) proves the split is re-read and
    # re-drawn on every call rather than served from the 30s cache.
    always_v2 = random.Random()
    always_v2.choices = lambda population, weights, k: [population[-1]]  # type: ignore[method-assign]
    first = prompts.get_prompt("triage", "prod", rng=always_v2).version
    second = prompts.get_prompt("triage", "prod", rng=always_v2).version
    assert first == v2
    assert second == v2


def test_split_falls_back_to_last_known_good_on_registry_outage(two_versions, monkeypatch):
    v1, v2 = two_versions
    set_prompt_split("triage", "prod", {v1: 1.0}, actor="me")
    pv = prompts.get_prompt("triage", "prod")  # seeds the fail-safe cache
    assert pv.version == v1

    def _boom(*_a, **_k):
        raise RuntimeError("registry down")

    monkeypatch.setattr("examlops.data.prompts.get_prompt_split", _boom)
    assert prompts.get_prompt("triage", "prod").version == v1


def test_set_prompt_label_clears_an_existing_split(two_versions):
    v1, v2 = two_versions
    set_prompt_split("triage", "prod", {v1: 0.5, v2: 0.5}, actor="me")
    assert get_prompt_split("triage", "prod") != []
    set_prompt_label("triage", "prod", v2)
    assert get_prompt_split("triage", "prod") == []
    assert prompts.get_prompt("triage", "prod").version == v2


def test_clear_prompt_split_reports_whether_one_existed(two_versions):
    v1, _v2 = two_versions
    assert clear_prompt_split("triage", "prod") is False
    set_prompt_split("triage", "prod", {v1: 1.0}, actor="me")
    assert clear_prompt_split("triage", "prod") is True
    assert get_prompt_split("triage", "prod") == []


def test_set_prompt_split_rejects_empty_and_nonpositive_weights(two_versions):
    v1, _v2 = two_versions
    with pytest.raises(ValueError, match="at least one"):
        set_prompt_split("triage", "prod", {}, actor="me")
    with pytest.raises(ValueError, match="positive"):
        set_prompt_split("triage", "prod", {v1: 0.0}, actor="me")
    with pytest.raises(ValueError, match="positive"):
        set_prompt_split("triage", "prod", {v1: -0.5}, actor="me")


def test_split_is_platform_db_only(two_versions):
    v1, _v2 = two_versions
    with use_backend("mlflow"):
        with pytest.raises(ValueError, match="platform_db"):
            set_prompt_split("triage", "prod", {v1: 1.0}, actor="me")
        assert get_prompt_split("triage", "prod") == []
        assert clear_prompt_split("triage", "prod") is False


# ── CLI ────────────────────────────────────────────────────────────────────


def test_cli_canary_sets_and_shows_split(two_versions):
    v1, v2 = two_versions
    result = runner.invoke(
        prompt_cmd.app, ["canary", "triage", "prod", "--split", f"{v1}:0.8,{v2}:0.2"]
    )
    assert result.exit_code == 0, result.output
    rows = get_prompt_split("triage", "prod")
    assert {int(r["version"]): r["weight"] for r in rows} == {v1: 0.8, v2: 0.2}


def test_cli_canary_clear(two_versions):
    v1, v2 = two_versions
    runner.invoke(prompt_cmd.app, ["canary", "triage", "prod", "--split", f"{v1}:0.5,{v2}:0.5"])
    result = runner.invoke(prompt_cmd.app, ["canary", "triage", "prod", "--clear"])
    assert result.exit_code == 0, result.output
    assert get_prompt_split("triage", "prod") == []


def test_cli_canary_needs_split_or_clear(two_versions):
    result = runner.invoke(prompt_cmd.app, ["canary", "triage", "prod"])
    assert result.exit_code == 1


def test_cli_canary_rejects_unknown_version(two_versions):
    result = runner.invoke(prompt_cmd.app, ["canary", "triage", "prod", "--split", "999:1.0"])
    assert result.exit_code == 1
    assert get_prompt_split("triage", "prod") == []


def test_cli_list_shows_canary_marker(two_versions):
    from examlops.cli import _output

    v1, v2 = two_versions
    runner.invoke(prompt_cmd.app, ["canary", "triage", "prod", "--split", f"{v1}:0.9,{v2}:0.1"])
    _output.json_mode = True
    try:
        result = runner.invoke(prompt_cmd.app, ["list", "triage"])
    finally:
        _output.json_mode = False
    assert result.exit_code == 0, result.output
    assert "prod" in result.output
    assert "splits" in result.output
