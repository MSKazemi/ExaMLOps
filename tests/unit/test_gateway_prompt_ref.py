"""LLM-serving resolves `name@label` through the prompt registry (ADR 0009 clause 3).

Clause 3 names two consumers, Skipper *and* LLM-serving. Skipper's half shipped in pass 188
(`skipper.prompts.system_prompt`); this is the serving half. The contract deliberately differs
in one place — see `test_an_unresolvable_reference_is_an_error_not_a_silent_omission`.
"""

import pytest

from examlops import prompts as reg
from examlops.data.prompts import create_prompt_version, set_prompt_label
from examlops.gateway import Completion, GatewayClient, Router, resolve_prompt_ref


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    reg.clear_cache()
    yield
    reg.clear_cache()


def _client(seen: list):
    def backend(model, messages, **kw):
        seen.append(messages)
        return Completion(text="ok", model=model, backend="stub")

    router = Router()
    router.add_route("m", [("stub", backend)])
    return GatewayClient(router=router)


def _seed(name="support-bot", template="BE HELPFUL", label="prod"):
    version = create_prompt_version(name, template, variables=[])
    set_prompt_label(name, label, version)
    return version


def test_resolve_prompt_ref_accepts_bare_name_and_name_at_label():
    _seed()
    assert resolve_prompt_ref("support-bot") == ("BE HELPFUL", "support-bot", 1)
    assert resolve_prompt_ref("support-bot@prod") == ("BE HELPFUL", "support-bot", 1)


def test_the_template_is_prepended_as_a_system_message():
    _seed()
    seen: list = []
    _client(seen).chat("m", [{"role": "user", "content": "hi"}], prompt_ref="support-bot")
    assert seen[0][0] == {"role": "system", "content": "BE HELPFUL"}
    assert seen[0][1] == {"role": "user", "content": "hi"}


def test_a_callers_own_system_message_is_never_replaced():
    """Rewriting a caller's messages would be a silent behaviour change."""
    _seed()
    seen: list = []
    caller = [{"role": "system", "content": "CALLER RULE"}, {"role": "user", "content": "hi"}]
    _client(seen).chat("m", caller, prompt_ref="support-bot")
    assert [m["content"] for m in seen[0]] == ["BE HELPFUL", "CALLER RULE", "hi"]


def test_the_callers_list_is_not_mutated():
    _seed()
    seen: list = []
    caller = [{"role": "user", "content": "hi"}]
    _client(seen).chat("m", caller, prompt_ref="support-bot")
    assert caller == [{"role": "user", "content": "hi"}]


def test_a_label_move_changes_what_is_served_with_no_caller_change():
    """The capability the registry exists for, on the serving path."""
    _seed()
    seen: list = []
    client = _client(seen)
    client.chat("m", [{"role": "user", "content": "hi"}], prompt_ref="support-bot@prod")

    v2 = create_prompt_version("support-bot", "BE TERSE", variables=[])
    set_prompt_label("support-bot", "prod", v2)
    reg.clear_cache()
    client.chat("m", [{"role": "user", "content": "hi"}], prompt_ref="support-bot@prod")

    assert seen[0][0]["content"] == "BE HELPFUL"
    assert seen[1][0]["content"] == "BE TERSE"


def test_an_unresolvable_reference_is_an_error_not_a_silent_omission():
    """Deliberately unlike Skipper, which falls back to its literal.

    A caller who names a prompt has no correct default. Sending the request without it
    would change the model's behaviour invisibly, so this fails loudly.
    """
    seen: list = []
    with pytest.raises(LookupError):
        _client(seen).chat("m", [{"role": "user", "content": "hi"}], prompt_ref="nope@prod")
    assert seen == []  # no backend was called


def test_omitting_prompt_ref_leaves_the_request_untouched():
    seen: list = []
    _client(seen).chat("m", [{"role": "user", "content": "hi"}])
    assert seen[0] == [{"role": "user", "content": "hi"}]
