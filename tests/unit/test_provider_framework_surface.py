"""ADR 0077 guards: platform-domain config home, ``exa providers list``, ``--placement-provider``."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from examlops.providers import loader


def test_non_finops_domain_reads_providers_yaml_and_env_wins(tmp_path, monkeypatch):
    """Clause 2: platform domains read providers.yaml; precedence env > config; finops.yaml apart."""
    from examlops import hpc_placement_providers  # noqa: F401 - registers 'placement'

    finops = tmp_path / "finops.yaml"
    finops.write_text("finops:\n  placement:\n    provider: nope-finops\n")
    provs = tmp_path / "providers.yaml"
    provs.write_text(
        'placement:\n  provider: expression\n  formulas:\n    score: "idle_gpus * 100 + idle_nodes"\n'
    )
    monkeypatch.setattr(loader, "FINOPS_YAML", finops)
    monkeypatch.setattr(loader, "PROVIDERS_YAML", provs)
    monkeypatch.delenv("EXAMLOPS_PLACEMENT_PROVIDER", raising=False)

    block = loader.load_domain_config("placement", group="platform")
    assert block["provider"] == "expression"  # providers.yaml, not the finops.yaml decoy
    assert loader.load_domain_config("placement")["provider"] == "nope-finops"
    p = loader.resolve_provider("placement", group="platform")
    assert p.compute({"idle_gpus": 2, "idle_nodes": 3}) == {"score": 203}

    monkeypatch.setenv("EXAMLOPS_PLACEMENT_PROVIDER", "least-loaded")
    assert (
        type(loader.resolve_provider("placement", group="platform")).__name__
        == "LeastLoadedProvider"
    )


def test_exa_providers_list_json_shows_placement_default():
    """Clause 4: ``exa providers list --domain placement --json`` enumerates with default marked."""
    from examlops.cli.main import app

    res = CliRunner().invoke(app, ["--json", "providers", "list", "--domain", "placement"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.stdout)
    assert any(r["name"] == "least-loaded" and r["default"] and r["ok"] for r in rows)


def test_hpc_place_exposes_placement_provider_option():
    """Clause 3: the CLI accepts ``--placement-provider``."""
    from examlops.cli.main import app

    res = CliRunner().invoke(app, ["hpc", "place", "--help"])
    assert res.exit_code == 0
    assert "--placement-provider" in res.output
