"""Per-domain data facades (enterprise-readiness Phase 4, item 4.5).

Proves every ``examlops.data.<domain>`` module re-exports its helpers as the *same* callables the
monolith exposes (non-destructive split — zero behaviour change), and that a facade actually works
end-to-end against a DB, so new code can depend on the narrow per-domain surface.
"""

from __future__ import annotations

import importlib

import pytest

_DOMAINS = [
    "agent",
    "coordination",
    "data_assets",
    "evaluation",
    "gateway",
    "governance",
    "prompts",
    "registry",
    "audit",
    "drift",
    "hpc",
    "projects",
    "finops",
    "serving",
    "events",
    "admission",
    "coordination",
    "secrets",
    "autopilot",
]


@pytest.mark.parametrize("domain", _DOMAINS)
def test_facade_reexports_are_identical_to_platform_db(domain):
    import examlops.platform_db as pdb

    # No reload: proxy facades resolve dynamically via __getattr__ (always current), and owned
    # facades hold the real def which platform_db re-exports — either way the identity below holds.
    mod = importlib.import_module(f"examlops.data.{domain}")
    assert mod.__all__, f"data.{domain} exports nothing"
    for name in mod.__all__:
        assert getattr(mod, name) is getattr(pdb, name), (
            f"data.{domain}.{name} is not the same object as platform_db.{name}"
        )


def test_facade_works_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb

    pdb.init_db()
    from examlops.data import audit, serving

    serving.set_traffic_rules("JPCP", {"Production": 90, "Canary": 10})
    assert serving.get_traffic_rules("JPCP") == {"Production": 90, "Canary": 10}
    audit.write_audit_event("test", "alice", "act", "JPCP")
    assert audit.verify_audit_chain()["ok"] is True


def test_data_package_lists_all_domains():
    import examlops.data as data

    assert set(_DOMAINS) <= set(data.__all__)
    assert "init_db" in data.__all__  # cross-cutting bootstrap re-exported at the root
