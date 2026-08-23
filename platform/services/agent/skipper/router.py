"""Deterministic specialist router (Phase 4, ADR 0100).

Picks which specialist handles a turn from the latest user message using cheap keyword/intent
matching — **no extra LLM call**, so it adds zero latency on a local 8B model. This is the
supervisor's routing brain in its default (``deterministic``) mode; an ``llm`` mode (a strong
backend acting as an LLM router) is a future enhancement gated on a hosted model being present.

Routing is intentionally conservative: it scores each specialist by trigger-keyword hits in the
message and returns the best match, falling back to the read-only ``general`` specialist when
nothing matches so an ambiguous request is never sent to a write-capable pack by accident.
"""

from __future__ import annotations

import re

from skipper import skills


def latest_user_text(messages: list) -> str:
    """Extract the most recent human message text from a LangGraph message list."""
    for msg in reversed(messages or []):
        role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else None)
        if role in ("human", "user"):
            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            if isinstance(content, list):  # multimodal → join text parts
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            return str(content or "")
    return ""


def score(text: str) -> dict[str, int]:
    """Trigger-keyword hit count per specialist (case-insensitive substring match)."""
    low = (text or "").lower()
    return {s.name: sum(1 for kw in s.triggers if kw in low) for s in skills.SPECIALISTS}


# Interrogative how-to phrasing means the user wants to *learn* how to do something (a HELP
# request) even when the sentence also names an action verb like "deploy" or "promote".
_HELP_MARKERS = ("how do i", "how to", "how can i", "how does", "what is", "explain how")

# *Capability* phrasing — "can I …?", "does it support …?" — is the same intent wearing different
# words, and it used to reach no specialist at all: none of these contain a `helper` trigger, so
# they scored zero everywhere and fell to the read-only generalist, which answered from the model's
# priors instead of the documentation. That is how Skipper came to recommend an LLM-judge promotion
# gate without mentioning that ADR 0111 refuses an uncalibrated judge.
_CAPABILITY_MARKERS = (
    "can i ",
    "could i ",
    "am i able",
    "is it possible",
    "is there a way",
    "do i need to",
    "does examlops",
    "does the platform",
    "do you support",
    "is there support for",
    "does it support",
)

# …but "can I see the drift?" is an *operational* request in the same clothing. When one of these
# verbs follows the marker the user wants the thing done, not explained, so the normal scoring
# decides and a domain specialist keeps the turn.
_OPERATIONAL_VERBS = (
    "see",
    "show",
    "get",
    "list",
    "check",
    "view",
    "run",
    "start",
    "stop",
    "restart",
    "have",
)

_MARKER_VERB = re.compile(
    r"\b(?:can|could)\s+(?:i|you|we)\s+(?:please\s+)?(" + "|".join(_OPERATIONAL_VERBS) + r")\b"
)


def _is_help_intent(low: str) -> bool:
    """Does this message ask to *learn* something, rather than to have it done?"""
    if any(m in low for m in _HELP_MARKERS):
        return True
    if any(m in low for m in _CAPABILITY_MARKERS):
        return not _MARKER_VERB.search(low)
    return False


def choose(text: str) -> str:
    """Return the specialist name for a message, or ``general`` when nothing matches.

    Help intent wins on interrogative phrasing — both "how do I …?" and "can I …?" are requests to
    *learn*, routed to the docs-RAG helper rather than to perform the named action. Otherwise the
    specialist with the most trigger hits wins; ties fall to ``general`` (read-only) rather than a
    write pack.
    """
    low = (text or "").lower()
    scores = score(low)
    if _is_help_intent(low):
        return "helper"
    best = max(scores, key=lambda n: scores[n], default="")
    if best and scores[best] > 0:
        return best
    return skills.GENERAL.name


def choose_from_messages(messages: list) -> str:
    return choose(latest_user_text(messages))
