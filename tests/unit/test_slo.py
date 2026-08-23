"""C6 — model-quality SLOs/SLIs & burn-rate alerting (ADR 0023).

GWT acceptance criteria from ``design/vision/specs/C6-model-quality-slos.md`` §6.
"""

from __future__ import annotations

import shutil
import subprocess
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


def test_gwt1_generate_rules_structure():
    """GWT-1: generated rules have recording + burn-rate groups with valid shape."""
    from examlops.slo import generate_rules

    spec = {
        "model": "JPCP",
        "name": "latency-p99",
        "target": 0.99,
        "window": "30d",
        "sli_query": "sum(rate(good[5m])) / sum(rate(total[5m]))",
    }
    rules = generate_rules(spec)
    assert len(rules.groups) == 2
    rec_group, alert_group = rules.groups
    records = [r["record"] for r in rec_group["rules"]]
    assert any("sli_ratio" in r for r in records)
    assert any("error_budget" in r for r in records)
    alerts = [r["alert"] for r in alert_group["rules"]]
    assert len(alerts) == 4  # four burn-rate windows
    # Fast-burn alert should be critical with a short `for`.
    fast = alert_group["rules"][0]
    assert fast["labels"]["severity"] == "critical"
    assert fast["for"] == "2m"


def test_gwt1_generate_rules_yaml_parses():
    from examlops.slo import generate_rules

    yaml = pytest.importorskip("yaml")
    rules = generate_rules({"model": "M", "name": "s", "target": 0.99})
    doc = yaml.safe_load(rules.to_yaml())
    assert "groups" in doc and len(doc["groups"]) == 2


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
def test_gwt1_promtool_validates(tmp_path):
    """GWT-1: promtool validates the generated rules when available.

    "When available" has so far meant *never*: promtool is not on the developer machines, the
    GitHub `examlops` job never installs it, and GitLab's `test:infra:alert-rules` job runs
    promtool against the static `alert_rules.yml` — a different artifact from these generated
    rules. Run by hand it passes, and it also passed while every alert's two windows were the
    same expression, because that is valid PromQL. Syntax is all this can speak to; what the
    alerts *mean* is guarded by `test_burn_rate_alerts_use_two_windows.py`, which needs no
    external binary and therefore runs everywhere.
    """
    from examlops.slo import generate_rules

    rules = generate_rules({"model": "M", "name": "s", "target": 0.99})
    f = tmp_path / "rules.yml"
    f.write_text(rules.to_yaml())
    res = subprocess.run(["promtool", "check", "rules", str(f)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_gwt3_quality_slo_burns_budget():
    """GWT-3: a groundedness SLO fed below target burns budget and reflects in status."""
    from examlops import platform_db
    from examlops.slo import apply_spec, slo_status

    apply_spec({"model": "LLM", "name": "groundedness", "target": 0.95, "sli_source": "c2"})
    # 90 good / 100 total = 0.90 SLI, below the 0.95 target => budget over-consumed.
    platform_db.record_slo_sample("LLM", "groundedness", good=90, total=100)

    st = slo_status("LLM", "groundedness")[0]
    assert st.sli == pytest.approx(0.90)
    assert not st.ok
    assert st.budget_remaining < 0  # 0.10 error > 0.05 budget => exhausted
    assert st.burn_rate > 1.0


def test_status_healthy_budget():
    from examlops import platform_db
    from examlops.slo import apply_spec, slo_status

    apply_spec({"model": "M", "name": "avail", "target": 0.99})
    platform_db.record_slo_sample("M", "avail", good=999, total=1000)  # 0.999 > 0.99
    st = slo_status("M", "avail")[0]
    assert st.ok
    assert st.budget_remaining > 0
    assert st.burn_rate < 1.0


def test_budget_exhausted_helper():
    from examlops import platform_db
    from examlops.slo import apply_spec, budget_exhausted

    apply_spec({"model": "M", "name": "err", "target": 0.99})
    platform_db.record_slo_sample("M", "err", good=80, total=100)  # 20% error >> 1% budget
    assert budget_exhausted("M", "err") is True

    apply_spec({"model": "M", "name": "ok", "target": 0.90})
    platform_db.record_slo_sample("M", "ok", good=99, total=100)
    assert budget_exhausted("M", "ok") is False


def test_spec_versioning():
    """R7: re-applying an SLO bumps its version."""
    from examlops.platform_db import get_slo_spec
    from examlops.slo import apply_spec

    apply_spec({"model": "M", "name": "s", "target": 0.99})
    apply_spec({"model": "M", "name": "s", "target": 0.995})
    spec = get_slo_spec("M", "s")
    assert spec["version"] == 2
    assert spec["target"] == 0.995


def test_gwt5_tenant_scoped():
    """GWT-5: SLOs are per-tenant (D6)."""
    from examlops import platform_db
    from examlops.slo import apply_spec, slo_status

    apply_spec({"model": "M", "name": "s", "target": 0.99, "tenant": "acme"})
    apply_spec({"model": "M", "name": "s", "target": 0.90, "tenant": "globex"})
    platform_db.record_slo_sample("M", "s", good=95, total=100, tenant="acme")
    platform_db.record_slo_sample("M", "s", good=95, total=100, tenant="globex")

    acme = slo_status("M", "s", tenant="acme")[0]
    globex = slo_status("M", "s", tenant="globex")[0]
    assert not acme.ok  # 0.95 < 0.99
    assert globex.ok  # 0.95 >= 0.90


def test_load_specs_yaml(tmp_path):
    pytest.importorskip("yaml")
    from examlops.slo import load_specs

    f = tmp_path / "slos.yaml"
    f.write_text(
        "slos:\n"
        "  - model: JPCP\n    name: latency-p99\n    target: 0.99\n"
        "  - model: JPCP\n    name: error-rate\n    target: 0.999\n"
    )
    specs = load_specs(str(f))
    assert len(specs) == 2
    assert specs[0]["name"] == "latency-p99"


def test_gwt4_slo_gate_blocks_promotion(monkeypatch):
    """GWT-4: budget exhausted + gate enabled => promote blocks (audited)."""
    from examlops import platform_db
    from examlops.cli.commands.slo_cmd import gate_enabled
    from examlops.slo import apply_spec, budget_exhausted

    monkeypatch.setenv("EXAMLOPS_SLO_GATE_ENABLED", "1")
    assert gate_enabled() is True

    apply_spec({"model": "JPCP", "name": "quality", "target": 0.99, "gate_promotion": True})
    platform_db.record_slo_sample("JPCP", "quality", good=50, total=100)

    gated = [
        s["name"]
        for s in platform_db.list_slo_specs(model="JPCP")
        if s["gate_promotion"] and budget_exhausted("JPCP", s["name"], tenant=s["tenant"])
    ]
    assert "quality" in gated


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r1 = runner.invoke(app, ["slo", "set", "JPCP", "latency-p99", "--target", "0.99"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["slo", "record", "JPCP", "latency-p99", "98", "100"])
    assert r2.exit_code == 0, r2.output
    r3 = runner.invoke(app, ["slo", "status", "JPCP"])
    assert r3.exit_code == 0, r3.output
    assert "latency-p99" in r3.output
    r4 = runner.invoke(app, ["slo", "generate", "JPCP", "latency-p99"])
    assert r4.exit_code == 0, r4.output
    assert "sli_ratio" in r4.output
