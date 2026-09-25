"""Production judge seam — the LLM-as-judge wired to the B2 model gateway (ADR 0007 d1/d4).

:class:`~examlops.evaluation.LLMJudge` takes any ``judge_fn``; until now nothing in the platform
built one, so every judge was a test lambda and "judges run at temperature 0" was a docstring.
This module is the real one:

* :func:`gateway_text_fn` — a ``(prompt, *, temperature=0.0) -> str`` generator that routes one
  chat request through :class:`examlops.gateway.GatewayClient`, so a judge call gets the same
  routing, virtual-key budget, guardrails, per-call cost and C1 span as every other LLM request.
  The request is sent with ``temperature=0`` and ``no_cache=True`` (a replicated judge that hits
  the semantic cache measures the cache — ADR 0111 G7.2). Any other temperature is **refused**.
* :func:`gateway_judge` — that generator plus :func:`parse_score`, i.e. a ``judge_fn`` returning
  a float in ``[0, 1]``. An answer carrying no usable score raises :class:`JudgeOutputError`
  rather than being read as 0: a judge that did not answer has not said "bad".

No paid API is implied: the gateway routes to whatever the operator registered (Ollama, a vLLM
endpoint, the echo route in tests).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from examlops.evaluation import JUDGE_TEMPERATURE, JudgeTemperatureError

#: Upper bound on the judge's answer. A score needs a handful of tokens; a cap bounds cost.
DEFAULT_MAX_TOKENS = 64

_RATIO = re.compile(r"(-?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
#: The scale the harness's own prompt names ("Score 0..1:"). A judge that echoes it before its
#: answer ("Score 0..1: 0.8") must not have the scale's 0 read as its score.
_SCALE_ECHO = re.compile(
    r"\b0\s*(?:\.\.|–|-|to)\s*1(?:\.0)?\b|\bbetween\s+0\s+and\s+1\b|\bin\s*\[0\s*,\s*1\]",
    re.IGNORECASE,
)


class JudgeOutputError(ValueError):
    """The judge answered, but not with a score in [0, 1]."""


def parse_score(text: str) -> float:
    """The score in a judge's answer: ``0.8``, ``Score: 0.8``, or a ratio such as ``4/5``.

    A bare number outside ``[0, 1]`` is refused rather than rescaled — "7" could be 7/10 or 7/100,
    and guessing the scale would put a fabricated number into a quality series.
    """
    text = _SCALE_ECHO.sub(" ", text)
    ratio = _RATIO.search(text)
    if ratio:
        num, den = float(ratio.group(1)), float(ratio.group(2))
        if den > 0 and 0.0 <= num <= den:
            return num / den
        raise JudgeOutputError(f"judge ratio out of range: {ratio.group(0)!r}")
    match = _NUMBER.search(text)
    if match is None:
        raise JudgeOutputError(f"judge answered no number: {text[:80]!r}")
    value = float(match.group(0))
    if not 0.0 <= value <= 1.0:
        raise JudgeOutputError(f"judge score {value} is outside [0, 1]")
    return value


def gateway_text_fn(
    model: str,
    *,
    client: Any = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    system: str | None = None,
) -> Callable[..., str]:
    """A temperature-0 text generator through the B2 gateway, for judges and DeepEval.

    ``client`` defaults to a :class:`~examlops.gateway.GatewayClient` over the platform's default
    router (built lazily on first call, so constructing a judge never touches the network).
    """
    state: dict[str, Any] = {"client": client}

    def _client() -> Any:
        if state["client"] is None:
            from examlops.gateway import GatewayClient, build_default_router

            state["client"] = GatewayClient(router=build_default_router())
        return state["client"]

    def generate(prompt: str, *, temperature: float = JUDGE_TEMPERATURE) -> str:
        if float(temperature) != JUDGE_TEMPERATURE:
            raise JudgeTemperatureError(
                f"judge generator asked for temperature={temperature}; ADR 0007 requires 0"
            )
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        completion = _client().chat(
            model,
            messages,
            no_cache=True,
            temperature=JUDGE_TEMPERATURE,
            max_tokens=max_tokens,
        )
        return str(getattr(completion, "text", completion))

    generate.temperature = JUDGE_TEMPERATURE  # type: ignore[attr-defined]
    return generate


def gateway_judge(
    model: str,
    *,
    client: Any = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Callable[..., float]:
    """A ``judge_fn`` for :class:`~examlops.evaluation.LLMJudge`, answered by ``model``."""
    generate = gateway_text_fn(
        model,
        client=client,
        max_tokens=max_tokens,
        system="You are an evaluation judge. Answer with a single number between 0 and 1.",
    )

    def judge(prompt: str, *, temperature: float = JUDGE_TEMPERATURE) -> float:
        return parse_score(generate(prompt, temperature=temperature))

    judge.temperature = JUDGE_TEMPERATURE  # type: ignore[attr-defined]
    judge.judge_model = model  # type: ignore[attr-defined]
    return judge


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "JudgeOutputError",
    "gateway_judge",
    "gateway_text_fn",
    "parse_score",
]
