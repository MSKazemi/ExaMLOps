"""ADR 0143 decision 9 — tool-call validity is enforced by policy; parse failures are tracked."""

from __future__ import annotations

import json

import pytest

from examlops.engines import tool_policy as tp
from examlops.engines.vllm_server import VLLMServerEngine

_TOOLS = [{"type": "function", "function": {"name": "get_job", "parameters": {"type": "object"}}}]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_TOOL_CHOICE_POLICY", raising=False)
    tp.reset_parse_stats()
    yield
    tp.reset_parse_stats()


@pytest.mark.parametrize("choice", [None, "auto", "none"])
def test_agent_tool_step_is_upgraded_to_required(choice):
    body = tp.apply_tool_choice_policy({}, tools=_TOOLS, tool_choice=choice, tool_step=True)
    assert body["tool_choice"] == "required"
    assert body["tools"] == _TOOLS


def test_a_named_function_choice_is_kept():
    named = {"type": "function", "function": {"name": "get_job"}}
    body = tp.apply_tool_choice_policy({}, tools=_TOOLS, tool_choice=named, tool_step=True)
    assert body["tool_choice"] == named


def test_non_tool_step_passes_the_callers_choice_through():
    body = tp.apply_tool_choice_policy({}, tools=_TOOLS, tool_choice="auto", tool_step=False)
    assert body["tool_choice"] == "auto"


def test_policy_off_leaves_auto_alone(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_TOOL_CHOICE_POLICY", "off")
    body = tp.apply_tool_choice_policy({}, tools=_TOOLS, tool_choice="auto", tool_step=True)
    assert body["tool_choice"] == "auto"


def test_a_typo_in_the_policy_fails_closed(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_TOOL_CHOICE_POLICY", "enfroce")
    assert tp.policy_mode() == "enforce"


def test_tool_choice_without_tools_is_refused_and_too_many_tools_too():
    with pytest.raises(ValueError, match="without any tools"):
        tp.apply_tool_choice_policy({}, tools=None, tool_choice="required")
    with pytest.raises(ValueError, match="at most"):
        tp.apply_tool_choice_policy({}, tools=_TOOLS * 200, tool_step=True)


def _call(args):
    return [{"type": "function", "function": {"name": "get_job", "arguments": args}}]


def test_validation_and_counting():
    assert tp.record_tool_calls(_call('{"id": 3}'))[0] is True
    ok, errs = tp.record_tool_calls(_call("{id: 3"))
    assert ok is False and "do not parse" in errs[0]
    assert tp.record_tool_calls(_call("[1, 2]"))[0] is False
    assert tp.record_tool_calls(None)[0] is False
    stats = tp.parse_stats()
    assert (stats["ok"], stats["parse_error"], stats["missing"], stats["total"]) == (1, 2, 1, 4)
    assert stats["failure_rate"] == 0.75
    lines = tp.prometheus_lines()
    assert 'examlops_tool_call_parse_total{outcome="parse_error"} 2' in lines


class _Resp:
    def __init__(self, data):
        self._b = json.dumps(data).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_server_engine_sends_required_and_counts_the_parse(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        return _Resp(
            {
                "choices": [
                    {
                        "message": {"content": None, "tool_calls": _call("not json")},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    eng = VLLMServerEngine("http://h:8000", "m", api_key="")
    comp = eng.chat([{"role": "user", "content": "go"}], tools=_TOOLS, tool_step=True)
    assert sent["body"]["tool_choice"] == "required"
    assert sent["body"]["tools"] == _TOOLS
    assert comp.tool_calls and comp.tool_calls[0]["function"]["name"] == "get_job"
    assert tp.parse_stats()["parse_error"] == 1


def test_plain_chat_sends_no_tool_fields(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        return _Resp({"choices": [{"message": {"content": "hi"}}], "usage": {}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    eng = VLLMServerEngine("http://h:8000", "m", api_key="")
    comp = eng.chat([{"role": "user", "content": "hi"}])
    assert "tools" not in sent["body"] and "tool_choice" not in sent["body"]
    assert comp.tool_calls is None
    assert tp.parse_stats()["total"] == 0
