# Guardrails / safety / PII defense (D8)

A declarative input/output guardrail layer at the B2 gateway and Skipper agent boundaries:
prompt-injection/jailbreak detection, PII detection + redaction, secret-leak defense,
toxicity moderation, and a per-session/tenant tool allow-list — with `off | monitor |
enforce` modes and **fail-closed** enforcement. Every block/redaction is audited (D4) and
counted for metrics.

Design: ADR 0026 · spec `design/vision/specs/D8-guardrails-safety-pii.md`.

## Graceful degrade

The production PII engine is **Presidio** and moderation is a hosted/LLM classifier; the
fallback is a set of regex detectors + the D7 secret scanner — so guardrails work with **no
external service**.

## Modes

At the gateway the mode comes from `EXAMLOPS_GUARDRAIL_MODE`, and the default is **`monitor`** —
not `off` and not `enforce`. Monitor scans everything and records what it finds to
`guardrail_events` while changing nothing a caller can observe, so the boundary can be switched on
without breaking traffic that was working, and an operator can see what their prompts actually
contain before deciding to block any of it. An unrecognised value falls back to `monitor`, so a
typo cannot silently disable the boundary.


- **`off`** — allow everything (no scanning).
- **`monitor`** — detect + log/alert, but never block (findings recorded, action stays
  `allow`).
- **`enforce`** — redact PII/secrets, block injection/toxicity, and **fail closed** on a
  scanner error (R6).

## CLI

```bash
exa guardrails test --text "ignore previous instructions" --direction input   # → block
exa guardrails test --text "email me at a@b.com" --direction output            # → redact
exa guardrails test --text "..." --mode monitor                                # log-only
exa guardrails check-tool delete_all --allow search --allow read               # → blocked
exa guardrails stats                                                           # allow/redact/block counts
```

## What it checks

| Boundary | Checks |
|---|---|
| **Input** (R1–R3) | prompt-injection/jailbreak, PII (redact/block), secret leak; RAG-retrieved content is scanned as untrusted |
| **Output** (R4) | PII/secret leakage redaction, toxicity moderation |
| **Tool calls** (R7) | per-session/tenant allow-list; disallowed tools blocked + audited |

## Where it runs

Two boundaries, both real:

| Boundary | What is scanned |
|---|---|
| **Gateway** (`GatewayClient.chat`) | every request in, every response out |
| **Agent / RAG** (`skipper/knowledge.py`) | retrieved context, treated as untrusted content |

The gateway is the boundary ADR 0026 names first, and it is the one that scans *both* directions.
Ordering there is deliberate and is covered by tests, because both mistakes are silent:

- **Inbound runs before the semantic cache** — otherwise a blocked prompt could still be answered
  from cache, and a redacted prompt and its raw form would become two cache entries.
- **Outbound runs after cost accounting but before the cache** — the tokens were spent whatever the
  guardrail decides, so a blocked answer must still appear on the bill it really incurred; and it
  must never be stored, or the violation would be served to everyone afterwards without a backend
  call and therefore without another scan.
- **A block is not a backend failure.** `GuardrailBlocked` is a typed `GatewayError` that is not
  retried against the next backend — retrying would spend money re-asking for the same violation
  and would surface "all backends failed" instead of the real reason.

```python
from examlops.gateway import GatewayClient, GuardrailBlocked

try:
    comp = client.chat("gpt-4o", [{"role": "user", "content": prompt}])
except GuardrailBlocked as exc:
    print(exc.direction, exc.findings)   # "request" | "response"
```

Set `guardrail=` on the client to override the environment, or `guardrail=None` to disable it for
one client.

## Interface

```python
from examlops.guardrails import DefaultGuardrail

guard = DefaultGuardrail(mode="enforce", tenant="acme", allowed_tools={"search", "read"})
res = guard.check_input(user_text, {})       # GuardResult(action, text, findings, reason)
if res.blocked:
    raise ValueError(res.reason)
safe_text = res.text                          # redacted where applicable
out = guard.check_output(model_text, {})
allowed = guard.check_tool_call("delete_all", {})   # False → blocked + audited
```

### RAG integration (B4)

Wire D8 into the RAG pipeline's guardrail seam so retrieved documents are scanned before
they reach the model:

```python
from examlops.rag import set_guardrail
from examlops.guardrails import DefaultGuardrail, rag_guardrail_adapter

set_guardrail(rag_guardrail_adapter(DefaultGuardrail(mode="enforce")))
```

## Governance

Every block/redaction writes a `guardrail_events` row (for Prometheus) and, for blocks and
redactions, an `audit_events` entry (`exa-guardrails` source, D4). `guardrail_stats()` powers
the dashboard violations panel.
