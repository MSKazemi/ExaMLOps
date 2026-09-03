"""Skipper resolves its system prompt through the prompt registry (ADR 0009 clause 3).

The registry shipped with no consumer: nothing under `platform/services/agent/` imported
`examlops.prompts`, so changing a prompt still needed a code deploy — verbatim the problem
ADR 0009 was written to solve. These tests hold the reader in place and, just as
importantly, hold the fail-safe in place: a broken or absent registry must never stop the
agent from starting.
"""

import sys
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[1]
if str(_AGENT) not in sys.path:
    sys.path.insert(0, str(_AGENT))
_CLI = _AGENT.parents[2] / "cli" / "src"
if str(_CLI) not in sys.path:
    sys.path.insert(0, str(_CLI))

import pytest  # noqa: E402
from skipper import prompts  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SKIPPER_PROMPT_REGISTRY", raising=False)
    monkeypatch.setenv("SKIPPER_PROMPT_LABEL", "prod")
    yield


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import prompts as reg

    reg.clear_cache()
    return reg


def test_the_consumers_no_longer_import_the_constant():
    """graph.py and supervisor.py must go through the resolver, not the literal."""
    for name in ("graph.py", "supervisor.py"):
        src = (_AGENT / "skipper" / name).read_text()
        assert "from skipper.prompts import system_prompt" in src, name
        assert "SYSTEM_PROMPT" not in src, f"{name} still reads the literal directly"


def test_falls_back_to_the_literal_when_the_registry_is_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert prompts.system_prompt() == prompts.SYSTEM_PROMPT


def test_a_broken_registry_does_not_stop_the_agent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    import examlops.prompts as reg

    def boom(*_a, **_k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(reg, "get_prompt", boom)
    assert prompts.system_prompt() == prompts.SYSTEM_PROMPT


def test_seeding_is_a_no_behaviour_change_then_a_label_move_changes_the_agent(
    monkeypatch, tmp_path
):
    """Clause 3's exact contract: seed as v1@prod (no change), then labels drive behaviour."""
    reg = _isolate(monkeypatch, tmp_path)

    version = prompts.seed_system_prompt(actor="test")
    assert version == 1
    reg.clear_cache()
    assert prompts.system_prompt() == prompts.SYSTEM_PROMPT  # seeding changed nothing

    assert prompts.seed_system_prompt(actor="test") is None  # idempotent

    from examlops.data.prompts import create_prompt_version, set_prompt_label

    v2 = create_prompt_version(prompts.PROMPT_NAME, "REVISED SYSTEM PROMPT", variables=[])
    set_prompt_label(prompts.PROMPT_NAME, "prod", v2)
    reg.clear_cache()
    assert prompts.system_prompt() == "REVISED SYSTEM PROMPT"  # no code deploy

    set_prompt_label(prompts.PROMPT_NAME, "prod", 1)  # rollback
    reg.clear_cache()
    assert prompts.system_prompt() == prompts.SYSTEM_PROMPT


def test_an_empty_version_is_a_mistake_not_an_instruction(monkeypatch, tmp_path):
    reg = _isolate(monkeypatch, tmp_path)
    from examlops.data.prompts import create_prompt_version, set_prompt_label

    v = create_prompt_version(prompts.PROMPT_NAME, "   \n  ", variables=[])
    set_prompt_label(prompts.PROMPT_NAME, "prod", v)
    reg.clear_cache()
    assert prompts.system_prompt() == prompts.SYSTEM_PROMPT


def test_the_registry_can_be_switched_off(monkeypatch, tmp_path):
    reg = _isolate(monkeypatch, tmp_path)
    prompts.seed_system_prompt()
    from examlops.data.prompts import create_prompt_version, set_prompt_label

    v2 = create_prompt_version(prompts.PROMPT_NAME, "REVISED", variables=[])
    set_prompt_label(prompts.PROMPT_NAME, "prod", v2)
    reg.clear_cache()
    monkeypatch.setenv("SKIPPER_PROMPT_REGISTRY", "0")
    assert prompts.system_prompt() == prompts.SYSTEM_PROMPT
