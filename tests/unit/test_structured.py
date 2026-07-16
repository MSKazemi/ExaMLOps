"""B8 — structured output & reasoning ops (ADR 0035)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"],
}


def test_validate_object_ok_and_errors():
    from examlops.structured import validate_object

    assert validate_object({"name": "a", "age": 3}, _SCHEMA) == []
    assert validate_object({"name": "a"}, _SCHEMA)  # missing age


def test_gwt1_generate_structured_valid_passthrough():
    from examlops.structured import generate_structured

    obj = generate_structured(
        "p", _SCHEMA, generate_fn=lambda p: {"name": "x", "age": 5}, model="m"
    )
    assert obj == {"name": "x", "age": 5}


def test_gwt2_repair_coerces_and_fills():
    from examlops.structured import generate_structured

    # age is a string "5" and an extra key — repair coerces + drops.
    obj = generate_structured(
        "p", _SCHEMA, generate_fn=lambda p: {"name": "x", "age": "5", "extra": 1}, model="m"
    )
    assert obj == {"name": "x", "age": 5}


def test_gwt2_unrepairable_raises():
    from examlops.structured import StructuredOutputError, generate_structured

    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    # generate returns a list — object repair fills required with 0, so it becomes valid.
    # Force a truly unrepairable case: a schema requiring a string that repair can satisfy?
    # Use max_repairs=0 to force failure on an invalid object.
    with pytest.raises(StructuredOutputError):
        generate_structured("p", schema, generate_fn=lambda p: {"wrong": 1}, max_repairs=0)


def test_structured_output_metered():
    from examlops.platform_db import structured_output_stats
    from examlops.structured import generate_structured

    generate_structured("p", _SCHEMA, generate_fn=lambda p: {"name": "x", "age": 5})
    generate_structured("p", _SCHEMA, generate_fn=lambda p: {"name": "x", "age": "5"})
    stats = structured_output_stats()
    assert stats.get("valid") == 1
    assert stats.get("repaired") == 1


def test_gwt4_reasoning_budget_cuts_off():
    from examlops.structured import ReasoningBudget

    allowed, cut = ReasoningBudget(1000).enforce(5000)
    assert allowed == 1000
    assert cut is True
    allowed, cut = ReasoningBudget(1000).enforce(500)
    assert allowed == 500
    assert cut is False


def test_gwt5_reasoning_accounted_separately():
    from examlops.platform_db import reasoning_usage_summary
    from examlops.structured import account_reasoning

    result = account_reasoning(
        "m", reasoning_tokens=1000, output_tokens=200, reasoning_rate=0.001, output_rate=0.002
    )
    assert result["reasoning_cost"] == pytest.approx(1.0)
    assert result["output_cost"] == pytest.approx(0.4)
    summary = reasoning_usage_summary("m")
    assert summary["reasoning_tokens"] == 1000
    assert summary["output_tokens"] == 200


def test_gwt6_trace_redacted_and_ttl():
    from examlops.structured import capture_reasoning_trace, get_reasoning_trace

    redacted = capture_reasoning_trace(
        "req-1", "reasoning about user@example.com", tenant="acme", ttl_seconds=100, now_ts=1000.0
    )
    assert "user@example.com" not in redacted  # PII redacted (D8)
    # Within TTL:
    assert get_reasoning_trace("req-1", now_ts=1050.0) is not None
    # After TTL:
    assert get_reasoning_trace("req-1", now_ts=2000.0) is None


def test_trace_tenant_scoped_stored():
    from examlops.platform_db import get_reasoning_trace
    from examlops.structured import capture_reasoning_trace

    capture_reasoning_trace("req-2", "some trace", tenant="acme")
    row = get_reasoning_trace("req-2")
    assert row["tenant"] == "acme"


def test_cli_smoke(tmp_path):
    import json

    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    schema_f = tmp_path / "schema.json"
    schema_f.write_text(json.dumps(_SCHEMA))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"name": "x", "age": 5}))
    r = runner.invoke(app, ["gateway", "schema", "test", str(schema_f), str(good)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "gateway",
            "reasoning",
            "account",
            "m",
            "--reasoning",
            "100",
            "--output",
            "50",
            "--reasoning-rate",
            "0.001",
            "--output-rate",
            "0.002",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["gateway", "reasoning", "budget", "5000", "--max", "1000"])
    assert r.exit_code == 0, r.output
    assert "cut off" in r.output.lower()
