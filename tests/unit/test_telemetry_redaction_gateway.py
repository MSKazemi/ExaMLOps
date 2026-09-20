"""ADR 0148 decision 2 — prompt/completion capture on the gateway path is redacted.

`EXAMLOPS_GENAI_CAPTURE_CONTENT` makes content leave the process on a span; the gateway used to
capture nothing and the redaction hook had no caller. These tests pin: enforce redacts PII and
secrets, monitor exports unchanged but records, off is identity, an unknown mode is enforce, and a
broken redactor drops the capture (fail closed) and is counted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import gateway as gw  # noqa: E402
from examlops import guardrails  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.telemetry import genai  # noqa: E402

_EMAIL = "alice@example.com"
_PROMPT = f"my email is {_EMAIL}, help"
_ANSWER = f"write to bob@example.org or {_EMAIL}"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_MODE", "off")  # isolate: only telemetry redaction
    monkeypatch.setenv("EXAMLOPS_GENAI_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.delenv("EXAMLOPS_TELEMETRY_REDACTION", raising=False)
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    init_db()
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider, raising=False)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(genai, "_REDACTION_FAILURES", 0)
    yield exporter


def _chat():
    router = gw.Router()
    router.add_route(
        "m",
        [("p", lambda model, messages, **kw: gw.Completion(text=_ANSWER, model=model, backend=""))],
    )
    return gw.GatewayClient(router=router).chat("m", [{"role": "user", "content": _PROMPT}])


def _attrs(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return dict(spans[0].attributes)


def test_default_enforce_redacts_prompt_and_completion(_env):
    comp = _chat()
    a = _attrs(_env)
    assert _EMAIL not in a["gen_ai.prompt"] and "[redacted-email]" in a["gen_ai.prompt"]
    assert _EMAIL not in a["gen_ai.completion"] and "bob@example.org" not in a["gen_ai.completion"]
    assert comp.text == _ANSWER  # the caller's answer is untouched by telemetry redaction


def test_latest_experimental_shape_is_redacted_too(_env, monkeypatch):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    _chat()
    a = _attrs(_env)
    assert _EMAIL not in a["gen_ai.input.messages"] and _EMAIL not in a["gen_ai.output.messages"]
    assert "gen_ai.prompt" not in a


def test_secret_is_redacted(_env):
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    text = f"creds {key} end"
    assert key not in guardrails.telemetry_redactor("t", "enforce")(text)


def test_monitor_exports_unchanged_and_records(_env):
    import os

    os.environ["EXAMLOPS_TELEMETRY_REDACTION"] = "monitor"
    try:
        _chat()
    finally:
        del os.environ["EXAMLOPS_TELEMETRY_REDACTION"]
    assert _EMAIL in _attrs(_env)["gen_ai.prompt"]
    from examlops.data import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT action, rule FROM guardrail_events WHERE direction='telemetry'"
        ).fetchall()
    assert rows and all(r[0] == "monitor" for r in rows)


def test_off_is_identity(_env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_TELEMETRY_REDACTION", "off")
    _chat()
    assert _attrs(_env)["gen_ai.prompt"] == _PROMPT


def test_unknown_mode_falls_back_to_enforce(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_TELEMETRY_REDACTION", "ofr")
    assert guardrails.telemetry_redaction_mode() == "enforce"


def test_broken_redactor_fails_closed_and_is_counted(_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("scanner down")

    monkeypatch.setattr(guardrails, "redact_pii", boom)
    comp = _chat()
    a = _attrs(_env)
    assert "gen_ai.prompt" not in a and "gen_ai.completion" not in a
    assert genai.redaction_failures() == 2
    assert comp.text == _ANSWER  # the request itself is still served


def test_unbuildable_redactor_fails_closed(_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no policy")

    monkeypatch.setattr(guardrails, "telemetry_redactor", boom)
    _chat()
    a = _attrs(_env)
    assert "gen_ai.prompt" not in a and genai.redaction_failures() == 1


def test_capture_off_by_default_still_captures_nothing(_env, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_GENAI_CAPTURE_CONTENT")
    _chat()
    assert "gen_ai.prompt" not in _attrs(_env)


def test_maybe_capture_content_raising_redactor_drops_only_that_piece(_env):
    class S:
        attrs: dict = {}

        def set_attribute(self, k, v):
            self.attrs[k] = v

    def only_prompt_breaks(t):
        if t == "p":
            raise ValueError
        return t.upper()

    s = S()
    assert genai.maybe_capture_content(s, prompt="p", completion="c", redactor=only_prompt_breaks)
    assert s.attrs == {"gen_ai.completion": "C"}
    assert genai.redaction_failures() == 1
