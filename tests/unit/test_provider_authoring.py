"""Notebook/dashboard-authored providers (ADR 0074): AST sandbox + per-project store.

The sandbox is security-critical — these tests assert it rejects the known escape hatches and that
the authoring store round-trips + registers providers so ``--provider <name>`` resolves.
"""

from __future__ import annotations

import pytest

from examlops.providers import (
    ProviderError,
    ProviderSecurityError,
    compile_provider,
    delete_provider,
    get_provider,
    list_project_providers,
    load_project_providers,
    provider_path,
    read_provider_source,
    register_from_source,
    save_provider,
    validate_source,
)

GOOD = """
class MyCost(Provider):
    name = "my-cost"
    def compute(self, inputs):
        return {"cost_usd": inputs.get("gpu_hours", 0) * 0.85 + inputs.get("cpu_hours", 0) * 0.05}
"""


@pytest.fixture(autouse=True)
def _providers_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PROVIDERS_DIR", str(tmp_path / "providers"))


# ── sandbox: allow good, reject escapes ───────────────────────────────────────


def test_good_provider_passes_and_computes():
    cls = compile_provider(GOOD)
    inst = cls()
    assert inst.name == "my-cost"
    assert inst.compute({"gpu_hours": 10, "cpu_hours": 100})["cost_usd"] == pytest.approx(13.5)


def test_math_is_injected_without_import():
    code = """
class P(Provider):
    name = "p"
    def compute(self, inputs):
        return {"v": math.sqrt(inputs.get("x", 0))}
"""
    cls = compile_provider(code)
    assert cls().compute({"x": 16})["v"] == pytest.approx(4.0)


@pytest.mark.parametrize(
    "snippet",
    [
        "import os",
        "from os import system",
        "import subprocess",
        "__import__('os')",
        "eval('1+1')",
        "exec('x=1')",
        "open('/etc/passwd')",
        "getattr(object, 'x')",
        "().__class__.__bases__[0].__subclasses__()",  # classic sandbox escape
        "x = (1).__class__",
        "globals()",
    ],
)
def test_sandbox_rejects_escapes(snippet):
    code = f"""
class P(Provider):
    name = "p"
    def compute(self, inputs):
        {snippet}
        return {{"v": 0}}
"""
    with pytest.raises(ProviderSecurityError):
        validate_source(code)


def test_syntax_error_is_security_error():
    with pytest.raises(ProviderSecurityError):
        validate_source("class P(Provider)\n  pass")


def test_no_provider_subclass_rejected():
    with pytest.raises(ProviderError, match="no Provider subclass"):
        compile_provider("x = 1")


def test_two_provider_subclasses_rejected():
    code = """
class A(Provider):
    name = "a"
    def compute(self, i): return {}
class B(Provider):
    name = "b"
    def compute(self, i): return {}
"""
    with pytest.raises(ProviderError, match="exactly one"):
        compile_provider(code)


# ── store round-trip + registration ───────────────────────────────────────────


def test_save_then_read_then_delete():
    save_provider("cost", "my-cost", GOOD, project="research", actor="alice")
    p = provider_path("research", "cost", "my-cost")
    assert p.exists()
    assert "MyCost" in read_provider_source("research", "cost", "my-cost")
    assert delete_provider("research", "cost", "my-cost") is True
    assert not p.exists()
    assert delete_provider("research", "cost", "my-cost") is False


def test_invalid_source_never_written():
    with pytest.raises(ProviderSecurityError):
        save_provider(
            "cost",
            "evil",
            "import os\nclass P(Provider):\n def compute(self,i): return {}",
            project="research",
        )
    assert not provider_path("research", "cost", "evil").exists()


def test_register_from_source_resolves_via_get_provider():
    register_from_source("cost", "nb-live", GOOD)
    prov = get_provider("cost", config={"provider": "nb-live"})
    assert prov.compute({"gpu_hours": 2})["cost_usd"] == pytest.approx(1.7)


def test_load_project_providers_registers_all():
    save_provider("cost", "c1", GOOD, project="research")
    save_provider(
        "carbon", "k1", GOOD.replace("MyCost", "K").replace("my-cost", "k1"), project="research"
    )
    results = load_project_providers("research")
    names = {(r["domain"], r["name"], r.get("registered")) for r in results}
    assert ("cost", "c1", True) in names
    assert ("carbon", "k1", True) in names
    # Now resolvable by name.
    assert get_provider("cost", config={"provider": "c1"}).compute({"gpu_hours": 1})["cost_usd"]


def test_list_flags_a_bad_file(tmp_path, monkeypatch):
    save_provider("cost", "ok", GOOD, project="research")
    # Hand-write a file that fails the gate (bypassing save_provider's validation).
    bad = provider_path("research", "cost", "bad")
    bad.write_text("import os\n", encoding="utf-8")
    listed = {r["name"]: r for r in list_project_providers("research")}
    assert listed["ok"]["ok"] is True
    assert listed["bad"]["ok"] is False and listed["bad"]["error"]


def test_path_traversal_rejected():
    with pytest.raises(ProviderError):
        provider_path("research", "cost", "../../etc/passwd")
    with pytest.raises(ProviderError):
        save_provider("cost", "..", GOOD, project="research")


def test_finops_cost_uses_authored_project_provider():
    # Author a project provider that returns a distinctive cost, then confirm the FinOps cost
    # entrypoint resolves it when the project is passed.
    from examlops.finops.cost import estimate_cost_via_provider

    code = """
class DoubleCost(Provider):
    name = "double"
    def compute(self, inputs):
        return {"cost_usd": inputs.get("gpu_hours", 0) * 2.0}
"""
    save_provider("cost", "double", code, project="research")
    got = estimate_cost_via_provider(5.0, provider="double", project="research")
    assert got["cost_usd"] == pytest.approx(10.0)
    assert got["provider"] == "double"
    # Without the project (and after a fresh registry it wouldn't be known) the default still works.
    base = estimate_cost_via_provider(5.0)
    assert "cost_usd" in base
