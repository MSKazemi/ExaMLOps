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
