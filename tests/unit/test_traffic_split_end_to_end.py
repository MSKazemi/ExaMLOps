"""Canary splits written by operators reach the inference router — against a real database.

Plan P0.4 / finding B4. `exa serve traffic JPCP --production 90 --canary 10` stored the rule under
``JPCP``; the router lowercased the name and looked up ``jpcp`` with a case-sensitive match; the
router's container read a private database anyway; and the CLI's live push went to a path that
404'd. Every existing router test mocked the database lookup, so none of the three could show up.
These tests go through the real ``platform_db`` (the conftest gives each test a private one) and the
real router resolution, the way a replica does.
"""

from __future__ import annotations

from collections import Counter

import pytest

from examlops.data import serving
from examlops.platform_db import get_db, init_db
from serving.inference_pipeline import app as pipeline


@pytest.fixture(autouse=True)
def _fresh_router_cache():
    pipeline._traffic_rules.clear()
    yield
    pipeline._traffic_rules.clear()


def _route_many(payload: dict, n: int = 2000) -> Counter:
    counts: Counter = Counter()
    for _ in range(n):
        counts[pipeline.ModelRouter._resolve(dict(payload))[1]] += 1
    return counts


def test_split_written_with_the_registry_name_reaches_the_router():
    serving.set_traffic_rules("JPCP", {"Production": 50, "Canary": 50}, updated_by="cli")

    counts = _route_many({"model_name": "JPCP", "alias": "Production"})

    assert set(counts) == {"Production", "Canary"}
    assert 700 < counts["Canary"] < 1300


def test_a_row_stored_before_the_canonical_key_still_resolves():
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO traffic_rules (model, rules, updated_by) VALUES "
            "('JPCP', '{\"Production\": 0, \"Canary\": 100}', 'legacy')"
        )

    assert serving.get_traffic_rules("jpcp") == {"Production": 0, "Canary": 100}


def test_rewriting_a_split_retires_the_other_casing():
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO traffic_rules (model, rules) VALUES ('JPCP', '{\"Production\": 100}')"
        )

    serving.set_traffic_rules("Jpcp", {"Production": 90, "Canary": 10})

    with get_db() as conn:
        models = [r[0] for r in conn.execute("SELECT model FROM traffic_rules")]
    # One row survives, under the spelling the latest writer used.
    assert models == ["Jpcp"]


def test_an_explicit_non_default_alias_is_not_overridden_by_the_split():
    serving.set_traffic_rules("JPCP", {"Production": 0, "Canary": 100})

    counts = _route_many({"model_name": "JPCP", "alias": "Staging"}, n=50)

    assert counts == Counter({"Staging": 50})


def test_default_alias_traffic_follows_the_split():
    # The bus bridge always sends the default alias, so this is the canary path in production.
    serving.set_traffic_rules("JPCP", {"Production": 0, "Canary": 100})

    assert _route_many({"model_name": "JPCP", "alias": "Production"}, n=50) == Counter(
        {"Canary": 50}
    )


def test_shadow_config_is_shared_and_canonical():
    stored = serving.set_shadow_config("JPCP", shadow_alias="Staging", updated_by="cli")

    assert stored == "JPCP"
    assert serving.get_shadow_config("JPCP")["shadow_alias"] == "Staging"
    serving.set_shadow_config("jpcp", enabled=False, updated_by="dashboard")
    assert serving.get_shadow_config("JPCP")["enabled"] == 0


def test_cli_push_targets_the_route_the_ingress_mounts():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    ingress = (root / "serving/ray_serving/app.py").read_text()
    assert 'route_prefix="/infer-pipeline"' in ingress
    for consumer in (
        "platform/cli/src/examlops/cli/commands/serve.py",
        "platform/services/agent/skipper/tools/platform_ops.py",
    ):
        source = (root / consumer).read_text()
        assert "/infer-pipeline/traffic-rules/" in source, consumer
        assert "_url}/traffic-rules/" not in source and "URL}/traffic-rules/" not in source
