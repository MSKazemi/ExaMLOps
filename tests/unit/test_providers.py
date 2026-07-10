"""Unit tests for the general pluggable-calculation substrate (examlops.providers).

Covers the Slice-0 substrate in isolation (no consumers wired): registry resolution & discovery,
the safe expression evaluator (including its sandbox), the declarative YAML provider, and the
config loader.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.providers import (  # noqa: E402
    ExpressionProvider,
    Provider,
    ProviderError,
    ProviderMeta,
)
from examlops.providers.expression import (  # noqa: E402
    evaluate_formula,
    evaluate_formulas,
)
from examlops.providers.loader import resolve_provider  # noqa: E402
from examlops.providers.registry import Registry  # noqa: E402

# ── a trivial built-in provider used across the registry tests ────────────────


class _Doubler(Provider):
    name = "doubler"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(methodology="x*2", outputs=("y",))

    def compute(self, inputs):
        return {"y": inputs["x"] * 2}


def _make_registry() -> Registry:
    reg = Registry()
    reg.register("demo", "doubler", _Doubler, default=True)
    return reg


# ── registry: registration, default, resolution ──────────────────────────────


def test_register_sets_default_and_resolves():
    reg = _make_registry()
    assert reg.default_name("demo") == "doubler"
    p = reg.get("demo")  # no name → default
    assert p.compute({"x": 21}) == {"y": 42}


def test_get_by_explicit_name_and_config_name():
    reg = _make_registry()
    assert reg.get("demo", "doubler").compute({"x": 1}) == {"y": 2}
    assert reg.get("demo", config={"provider": "doubler"}).compute({"x": 3}) == {"y": 6}


def test_unknown_provider_raises_with_available_list():
    reg = _make_registry()
    with pytest.raises(ProviderError) as exc:
        reg.get("demo", "nope")
    assert "doubler" in str(exc.value)


def test_no_default_no_name_raises():
    reg = Registry()
    with pytest.raises(ProviderError):
        reg.get("empty")


def test_explicit_default_flag_overrides_first_registered():
    reg = Registry()
    reg.register("d", "a", _Doubler)
    reg.register("d", "b", _Doubler, default=True)
    assert reg.default_name("d") == "b"


def test_callable_factory_receives_config():
    reg = Registry()

    def factory(config):
        mult = config.get("mult", 1)

        class _P(Provider):
            name = "scaler"

            def compute(self, inputs):
                return {"y": inputs["x"] * mult}

        return _P()

    reg.register("demo", "scaler", factory)
    assert reg.get("demo", "scaler", {"mult": 5}).compute({"x": 2}) == {"y": 10}


def test_factory_not_returning_provider_raises():
    reg = Registry()
    reg.register("demo", "bad", lambda cfg: object())
    with pytest.raises(ProviderError):
        reg.get("demo", "bad")


# ── discovery ─────────────────────────────────────────────────────────────────


def test_discover_lists_builtins_marked_default():
    reg = _make_registry()
    infos = reg.discover("demo")
    assert [i.name for i in infos] == ["doubler"]
    assert infos[0].kind == "builtin" and infos[0].ok
    assert infos[0].value == "default"


def test_discover_captures_broken_entrypoint(monkeypatch):
    reg = _make_registry()

    class _EP:
        name = "brokenplugin"
        value = "bad_pkg:thing"

        def load(self):
            raise RuntimeError("boom")

    monkeypatch.setattr("examlops.providers.registry._entry_points", lambda group: [_EP()])
    infos = reg.discover("demo")
    broken = [i for i in infos if i.name == "brokenplugin"][0]
    assert broken.ok is False and "boom" in broken.error and broken.kind == "entrypoint"


def test_get_resolves_entrypoint_plugin(monkeypatch):
    reg = Registry()  # no builtins

    class _EP:
        name = "extra"
        value = "x:y"

        def load(self):
            return _Doubler

    monkeypatch.setattr("examlops.providers.registry._entry_points", lambda group: [_EP()])
    assert reg.get("demo", "extra").compute({"x": 4}) == {"y": 8}


# ── safe expression evaluation ────────────────────────────────────────────────


def test_evaluate_formula_arithmetic():
    assert (
        evaluate_formula(
            "gpu_hours * (tdp / 1000) * pue", {"gpu_hours": 10, "tdp": 400, "pue": 1.5}
        )
        == 6.0
    )


def test_evaluate_formula_allows_math_funcs():
    assert evaluate_formula("max(a, b) + sqrt(c)", {"a": 1, "b": 4, "c": 9}) == 7.0


def test_formulas_chain_outputs():
    out = evaluate_formulas(
        {"kwh": "gpu_hours * (tdp / 1000) * pue", "co2e_g": "kwh * grid"},
        {"gpu_hours": 10, "tdp": 400, "pue": 1.5, "grid": 300},
    )
    assert out == {"kwh": 6.0, "co2e_g": 1800.0}


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo hi')",
        "(1).__class__.__bases__",
        "open('/etc/passwd')",
    ],
)
def test_expression_sandbox_rejects_dangerous_input(expr):
    with pytest.raises(ProviderError):
        evaluate_formula(expr, {})


def test_expression_missing_simpleeval_gives_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "simpleeval":
            raise ImportError("no simpleeval")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ProviderError) as exc:
        evaluate_formula("1+1", {})
    assert "examlops[finops]" in str(exc.value)


# ── declarative YAML/expression provider ──────────────────────────────────────


def test_expression_provider_round_trip():
    p = ExpressionProvider(
        "cfg",
        formulas={"kwh": "gpu_hours * (tdp / 1000) * pue", "co2e_g": "kwh * grid"},
        coefficients={"tdp": 400, "pue": 1.5, "grid": 300},
        meta=ProviderMeta(methodology="test", uncertainty=0.25),
    )
    assert p.compute({"gpu_hours": 10}) == {"kwh": 6.0, "co2e_g": 1800.0}
    assert p.metadata().uncertainty == 0.25


def test_input_overrides_configured_coefficient():
    p = ExpressionProvider("cfg", {"kwh": "gpu_hours * pue"}, {"pue": 1.5})
    assert p.compute({"gpu_hours": 10, "pue": 2.0}) == {"kwh": 20.0}  # input wins


def test_expression_provider_requires_formulas():
    with pytest.raises(ProviderError):
        ExpressionProvider("cfg", formulas={})


def test_registry_builds_inline_expression_provider():
    reg = Registry()
    block = {
        "provider": "expression",
        "coefficients": {"tdp": 400, "pue": 1.5, "grid": 300},
        "formulas": {"kwh": "gpu_hours * (tdp / 1000) * pue", "co2e_g": "kwh * grid"},
        "metadata": {"uncertainty": 0.2, "methodology": "inline"},
    }
    p = reg.get("carbon", config=block)
    assert p.compute({"gpu_hours": 10})["co2e_g"] == 1800.0
    assert p.metadata().uncertainty == 0.2


# ── config loader ─────────────────────────────────────────────────────────────


def test_resolve_provider_uses_config_block():
    # register a builtin on the *global* registry the loader consults
    from examlops.providers import register_provider

    register_provider("loadertest", "doubler", _Doubler, default=True)
    p = resolve_provider("loadertest", config={})
    assert p.compute({"x": 5}) == {"y": 10}


def test_resolve_provider_env_override(monkeypatch):
    from examlops.providers import register_provider

    register_provider("loaderenv", "doubler", _Doubler, default=True)
    reg_block = {
        "provider": "expression",
        "formulas": {"y": "x + 1"},
    }
    # env var forces a specific name; here point back at the builtin to prove precedence
    monkeypatch.setenv("EXAMLOPS_LOADERENV_PROVIDER", "doubler")
    p = resolve_provider("loaderenv", config=reg_block)
    assert p.compute({"x": 7}) == {"y": 14}  # doubler, not the x+1 expression


def test_load_domain_config_reads_finops_yaml(tmp_path, monkeypatch):
    from examlops.providers import loader

    yaml_file = tmp_path / "finops.yaml"
    yaml_file.write_text(
        "finops:\n"
        "  carbon:\n"
        "    provider: expression\n"
        "    coefficients: {tdp: 700, pue: 1.3, grid: 250}\n"
        "    formulas:\n"
        '      kwh: "gpu_hours * (tdp / 1000) * pue"\n'
        '      co2e_g: "kwh * grid"\n'
    )
    monkeypatch.setattr(loader, "FINOPS_YAML", yaml_file)
    block = loader.load_domain_config("carbon")
    assert block["provider"] == "expression"
    p = resolve_provider("carbon", config=block)
    assert p.compute({"gpu_hours": 10})["kwh"] == pytest.approx(9.1)


def test_load_domain_config_missing_file_is_empty(tmp_path, monkeypatch):
    from examlops.providers import loader

    monkeypatch.setattr(loader, "FINOPS_YAML", tmp_path / "nope.yaml")
    assert loader.load_domain_config("carbon") == {}


def test_load_domain_config_malformed_yaml_is_empty(tmp_path, monkeypatch):
    from examlops.providers import loader

    bad = tmp_path / "finops.yaml"
    bad.write_text("finops: [unclosed\n")
    monkeypatch.setattr(loader, "FINOPS_YAML", bad)
    assert loader.load_domain_config("carbon") == {}


def test_get_resolves_dotted_import_path():
    reg = Registry()
    # point at this test module's _Doubler via a dotted path
    p = reg.get("demo", "tests.unit.test_providers:_Doubler")
    assert p.compute({"x": 6}) == {"y": 12}


def test_dotted_import_bad_path_raises():
    reg = Registry()
    with pytest.raises(ProviderError):
        reg.get("demo", "no.such.module:Thing")
