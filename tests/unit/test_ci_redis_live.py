"""CI contract for the opt-in live Redis coordination evidence suite."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _job() -> dict:
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    return pipeline["test:integration"]


def test_integration_job_provides_ephemeral_redis_with_a_readiness_gate():
    job = _job()
    services = job.get("services", [])
    assert {service.get("name"): service.get("alias") for service in services}[
        "redis:7.4-alpine"
    ] == "redis"

    script = "\n".join(job["script"])
    assert "redis.Redis.from_url" in script
    assert ".ping()" in script
    assert "Redis service did not become ready within 30 seconds" in script


def test_live_url_is_scoped_to_the_redis_test_invocation_only():
    job = _job()
    assert "EXAMLOPS_REDIS_TEST_URL" not in job.get("variables", {})

    script = "\n".join(job["script"])
    offline = ".venv/bin/pytest tests/integration/ -v --tb=short"
    live = ".venv/bin/pytest tests/integration/test_redis_coordination_live.py"
    assert offline in script
    assert live in script
    assert script.index(offline) < script.index("REDIS_TEST_URL=") < script.index(live)
    assert 'EXAMLOPS_REDIS_TEST_URL="$REDIS_TEST_URL"' in script


def test_live_redis_report_and_optional_dependency_are_wired():
    job = _job()
    assert 'uv pip install -e "platform/cli[coordination]"' in job["before_script"]
    junit = job["artifacts"]["reports"]["junit"]
    assert "reports/integration.xml" in junit
    assert "reports/integration-redis.xml" in junit
