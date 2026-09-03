"""What did the answers cost?

The four agent suites — operator-QA, CLI-coverage, grounding, safety — score whether the agent is
*right*, and since 2026-08-28 how long it took. Neither number says what a run consumed. An agent
that answers correctly by spending 40 000 tokens per question is not the same product as one that
answers correctly on 4 000, and nothing in a stored result told the two apart. This module turns
the ``usage`` block the bridge already returns (``skipper/oai_compat.py`` maps LangChain's
``usage_metadata`` onto the OpenAI shape) into scores, which makes a token budget a plain
``exa eval gate set … --metric tokens_per_answer:max=6000`` rather than new gate machinery.

Two rules keep the numbers honest, both learned from the suites already here:

**A budget that cannot be measured must not be recorded.** ``_latency_scores`` returns ``{}``
rather than a zero when nothing was timed, for the reason that a stored zero is indistinguishable
from a genuinely free run. Tokens are worse than latency here, because the gate direction is a
*ceiling*: if the bridge stops reporting ``usage``, a summed ``tokens_total`` falls toward zero and
a ``max=`` budget gate passes precisely when measurement has broken. So no usage reported ⇒ no
token scores, and partial coverage is recorded as ``usage_reported_rate`` so a total that came
from three of thirty answers is never read as a total over thirty.

**A price is only reported when a price is actually known.** ``estimate_cost`` in
``telemetry.genai`` charges unknown models a conservative default rate, which is right for a span
attribute and wrong here: Skipper's normal backend is a local Ollama model, and running it through
a generic default invents a dollar figure for self-hosted inference — the exact trap
``estimate_llm_cost_via_provider`` documents when it declines to guess. Tokens are observed and
always recorded; ``cost_usd`` appears only when an operator selected an ``llm_cost`` provider or
the model is one the built-in table actually prices. On an HPC-sovereign platform the honest
answer to "what did that cost in USD" is usually *no such number*, and the GPU-seconds road
(``exa finops``) is the one that answers it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The scores below that carry a **unit** — tokens, dollars — rather than a share of a sample.
#: Passed to ``record_eval_result`` so it does not wrap them in a Wilson proportion interval,
#: which is defined for *k of n* and says nothing about a price. ``usage_reported_rate`` is
#: deliberately absent: it is a genuine k/n and keeps its interval.
UNIT_METRICS = frozenset(
    {
        "tokens_total",
        "prompt_tokens_total",
        "completion_tokens_total",
        "tokens_per_answer",
        "cost_usd",
        "cost_per_answer",
    }
)


@dataclass(frozen=True)
class Usage:
    """Token counts for one answer, in the OpenAI ``usage`` shape."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


def _as_int(value: Any) -> int:
    """A count, or 0 for anything that is not one. Never raises — a malformed field must not
    end a suite run that has already spent real time and real tokens getting the answer."""
    try:
        out = int(value)
    except (TypeError, ValueError):
        return 0
    return out if out >= 0 else 0


def parse_usage(payload: Mapping[str, Any] | None) -> Usage | None:
    """Read the ``usage`` block off one chat-completion response body.

    ``None`` when the block is missing, malformed, or all-zero. All-zero counts as absent on
    purpose: the bridge omits ``usage`` entirely when the model reported no ``usage_metadata``,
    but a backend that sends a zero-filled block instead is making the same statement, and only
    one of those two spellings should reach the scores.
    """
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None
    prompt = _as_int(raw.get("prompt_tokens"))
    completion = _as_int(raw.get("completion_tokens"))
    # A backend that reports only a total is still telling us what the answer cost.
    total = _as_int(raw.get("total_tokens")) or (prompt + completion)
    if total == 0:
        return None
    return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


def pricing_names(model: str) -> list[str]:
    """The names to try against a rate table, most specific first.

    A recorded ``model_version`` is ``"<backend>:<model>"`` (``_agent_backend``), so the string
    that identifies the run — ``"ollama:llama3.1:8b"`` — is not the string a rate table is keyed
    by. Stripping to the last colon-segment would be wrong in the other direction, because model
    names carry their own colons and ``"8b"`` prices nothing.

    So: the full string first, then everything after the *first* colon. ``"llama3.1:8b"`` matches
    on the first try and never reaches the second; ``"ollama:llama3.1:8b"`` matches on the second.
    A bare unknown name simply matches neither, which is the outcome that must stay cheap.
    """
    names = [model]
    if ":" in model:
        names.append(model.split(":", 1)[1])
    return names


def price(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """USD for the run, or ``None`` when this model has no known price.

    Order mirrors the gateway's live path (ADR 0083): the operator-selected ``llm_cost``
    provider first, so a site can price its own contract without touching core code, then the
    built-in rate table — but only for a model that table actually names. The unknown-model
    default rate is deliberately not used; see the module docstring.
    """
    for name in pricing_names(model):
        try:
            from examlops.llmops_providers import estimate_llm_cost_via_provider

            chosen = estimate_llm_cost_via_provider(
                model=name, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            )
            if chosen is not None:
                return round(float(chosen), 6)
        except Exception:
            # A bad plugin or config must not fail a suite that has already run.
            pass
        try:
            from examlops.telemetry.genai import estimate_cost, has_rate

            if has_rate(name):
                return round(float(estimate_cost(name, prompt_tokens, completion_tokens)), 6)
        except Exception:
            pass
    return None


def usage_scores(
    usages: list[Usage | None],
    *,
    model: str,
    answered: int | None = None,
) -> dict[str, float]:
    """Token — and where priced, cost — scores for one suite run.

    ``usages`` is one entry per answer that came back, ``None`` where that answer carried no
    usage block, so coverage is computed against the answers rather than against the questions
    asked (a question that timed out cost tokens nobody can attribute, and is already visible in
    every suite's ``answer_rate``). ``answered`` overrides that denominator only if a caller
    counts answers differently from the list it passes.

    Returns ``{}`` when no answer reported usage at all.
    """
    reported = [u for u in usages if u is not None]
    total_answers = len(usages) if answered is None else answered
    if not reported or total_answers <= 0:
        return {}

    prompt_total = sum(u.prompt_tokens for u in reported)
    completion_total = sum(u.completion_tokens for u in reported)
    tokens_total = sum(u.total_tokens for u in reported)

    scores: dict[str, float] = {
        "tokens_total": float(tokens_total),
        "prompt_tokens_total": float(prompt_total),
        "completion_tokens_total": float(completion_total),
        # Per *reported* answer, not per answer: dividing a total that covers three answers by
        # thirty would report a fifth of the real per-question spend and pass any ceiling.
        "tokens_per_answer": round(tokens_total / len(reported), 1),
        # The number that says how much of the run the totals above actually cover.
        "usage_reported_rate": round(len(reported) / total_answers, 4),
    }

    cost = price(model, prompt_total, completion_total)
    if cost is not None:
        scores["cost_usd"] = cost
        scores["cost_per_answer"] = round(cost / len(reported), 6)
    return scores
