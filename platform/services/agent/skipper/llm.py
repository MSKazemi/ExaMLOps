from __future__ import annotations

import httpx

from skipper import config

# The backend `check_backend()` last found *usable*, as a type string. Stored as a string, not a
# closure or a client, so it can never carry stale credentials: `build_llm()` re-derives everything
# from the current config and ignores a type that is no longer configured.
_RESOLVED_TYPE: str | None = None


def reset_resolved() -> None:
    """Forget the probed backend. For tests, and for a config reload."""
    global _RESOLVED_TYPE
    _RESOLVED_TYPE = None


def _configured_types() -> list[str]:
    """The backends this process could use, in preference order. Ollama is always last."""
    types = []
    if config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_ENDPOINT:
        types.append("azure")
    if config.ANTHROPIC_API_KEY:
        types.append("claude")
    types.append("ollama")
    return types


def build_llm(model: str | None = None):
    """Build the LLM backend.

    Preference order: Azure OpenAI / Foundry (AZURE_OPENAI_API_KEY +
    AZURE_OPENAI_ENDPOINT) → Claude API (ANTHROPIC_API_KEY) → Ollama. Claude
    uses adaptive thinking so the model decides when to reason step-by-step.

    **Preferred means preferred-when-usable.** If :func:`check_backend` has already probed the
    candidates, this builds whichever one it found *working* — not merely whichever one has
    non-empty environment variables. Without that, a rejected key produced a client that raised
    ``AuthenticationError`` on the first token while a working backend sat unused behind it, which
    is how the agent went down on 2026-08-20. This function itself performs **no** I/O: the probing
    belongs at the process entry points, which already call ``check_backend()`` at startup.
    """
    chosen = _RESOLVED_TYPE if _RESOLVED_TYPE in _configured_types() else None
    if chosen == "azure" or (
        chosen is None and config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_ENDPOINT
    ):
        from langchain_openai import ChatOpenAI

        # The Foundry v1 endpoint is OpenAI-compatible: base_url + api_key, with
        # the deployment name used as the model id. Temperature is left unset —
        # gpt-5.x reasoning models reject anything other than the default.
        # api_key/model are accepted as plain str at runtime (pydantic coerces);
        # the stub types api_key as SecretStr, hence the ignore.
        return ChatOpenAI(
            model=model or config.AZURE_OPENAI_DEPLOYMENT,
            base_url=config.AZURE_OPENAI_ENDPOINT,
            api_key=config.AZURE_OPENAI_API_KEY,  # type: ignore[arg-type]
        )
    if chosen == "claude" or (chosen is None and config.ANTHROPIC_API_KEY):
        from langchain_anthropic import ChatAnthropic

        # model/anthropic_api_key/max_tokens are valid ChatAnthropic pydantic
        # fields at runtime; the shipped stub omits them from __init__.
        return ChatAnthropic(  # type: ignore[call-arg]
            model=model or config.ANTHROPIC_MODEL,
            anthropic_api_key=config.ANTHROPIC_API_KEY,
            thinking={"type": "adaptive"},
            max_tokens=16000,
        )
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=model or config.AGENT_MODEL,
        base_url=config.AGENT_OLLAMA_URL,
        temperature=0,
        keep_alive=config.AGENT_OLLAMA_KEEP_ALIVE,
        reasoning=config.AGENT_OLLAMA_REASONING,
        num_ctx=config.AGENT_OLLAMA_NUM_CTX or None,
    )


# Statuses meaning "the endpoint is alive but refused our credential". A live
# gateway answering 401 cannot serve a single token, so it must report unhealthy:
# to an operator, a healthy-but-unusable backend is worse than an obviously dead
# one, because it sends them looking for the fault everywhere except the key.
_AUTH_REJECTED = frozenset({401, 403})


def _endpoint_reachable(url: str, *, timeout: float = 5.0, headers: dict | None = None) -> bool:
    """True if *url* answers within *timeout* and does not reject our credential.

    Three outcomes, two of them unhealthy:

    * transport error (DNS/connect/timeout) -> the endpoint is down;
    * ``401``/``403`` -> the endpoint is up but the key is invalid, revoked, or
      issued for a different resource, which is just as fatal for the agent;
    * anything else (``200``, ``404``, ``5xx``) -> routable, treated as reachable.
      A non-auth status is deliberately *not* fatal because providers expose
      different probe paths, and a ``404`` on ``/models`` says nothing about
      whether chat completions works.

    Verified against the live LXP deployment on 2026-08-20: the Foundry endpoint
    answered ``401`` to the stored key while ``check_backend()`` still reported
    ``ok: True``, so the agent advertised a backend it could not use at all.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers or {})
    except httpx.RequestError:
        return False
    return resp.status_code not in _AUTH_REJECTED


def _probe(backend_type: str) -> bool:
    """Is this backend usable *right now* — routable and not refusing our credential?"""
    if backend_type == "azure":
        # Probe /models *with* the key. Probing the bare endpoint unauthenticated
        # returned the same 401 whether or not a key was supplied, so it could
        # never distinguish a good key from a revoked one.
        return _endpoint_reachable(
            config.AZURE_OPENAI_ENDPOINT.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {config.AZURE_OPENAI_API_KEY}"},
        )
    if backend_type == "claude":
        return _endpoint_reachable(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
    return _endpoint_reachable(f"{config.AGENT_OLLAMA_URL}/api/tags")


def _model_of(backend_type: str) -> str:
    return {
        "azure": config.AZURE_OPENAI_DEPLOYMENT,
        "claude": config.ANTHROPIC_MODEL,
    }.get(backend_type, config.AGENT_MODEL)


# Which environment variable an operator has to fix, per backend. The CLI used to print
# "Ollama not reachable" whatever had actually failed, which sent them looking everywhere
# except at the credential that was rejected.
_FIX_HINT = {
    "azure": "AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT (endpoint must be the "
    "OpenAI-compatible base, e.g. https://<resource>.openai.azure.com/openai/v1, and "
    "AZURE_OPENAI_DEPLOYMENT must be a deployment *name*)",
    "claude": "ANTHROPIC_API_KEY",
    "ollama": "AGENT_OLLAMA_URL (start it with 'ollama-tunnel start')",
}


def check_backend() -> dict:
    """Return the backend that will actually be used: ``{ok, type, model}``.

    Two behaviours, both learned from outages:

    * **Every backend is actively probed.** Azure/Claude used to be assumed healthy from mere
      env-var presence, so a dead endpoint reported ``ok: True`` and masked the outage.
    * **A rejected backend is skipped, not merely reported.** The candidates are tried in
      preference order and the first *usable* one wins, so one stale key no longer takes the agent
      down while a working backend sits behind it. The choice is remembered for
      :func:`build_llm`, which is what keeps the reported backend and the used backend the same.

    When nothing is usable the **preferred** candidate is returned with ``ok: False`` — that is the
    one whose configuration the operator meant to use, so it is the one worth naming. ``skipped``
    lists the candidates tried and rejected before it, and ``fix`` names the variable to repair.
    """
    global _RESOLVED_TYPE
    candidates = _configured_types()
    skipped: list[str] = []
    for backend_type in candidates:
        if _probe(backend_type):
            _RESOLVED_TYPE = backend_type
            info = {"ok": True, "type": backend_type, "model": _model_of(backend_type)}
            if skipped:
                info["skipped"] = skipped
            return info
        skipped.append(backend_type)
    _RESOLVED_TYPE = None
    preferred = candidates[0]
    return {
        "ok": False,
        "type": preferred,
        "model": _model_of(preferred),
        "skipped": skipped,
        "fix": _FIX_HINT[preferred],
    }


def check_ollama() -> bool:
    """Backward-compatible shim used by legacy callers."""
    return check_backend()["ok"]
