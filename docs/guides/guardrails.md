# Guardrails / safety / PII defense (D8)

A declarative input/output guardrail layer at the B2 gateway and Skipper agent boundaries:
prompt-injection/jailbreak detection, PII detection + redaction, secret-leak defense,
toxicity moderation, and a per-session/tenant tool allow-list — with `off | monitor |
enforce` modes and **fail-closed** enforcement. Every block/redaction is audited (D4) and
counted for metrics.

Design: ADR 0026 · spec `design/vision/specs/D8-guardrails-safety-pii.md`.

## Graceful degrade

Moderation is a hosted/LLM classifier in production, not built; the regex detectors + the D7
secret scanner are what every deployment runs by default — so guardrails work with **no
external service**. That makes their exact coverage worth stating rather than leaving to the
word "PII":

| Detected (always, regex) | Detected (opt-in, Presidio NER — see below) | Not detected |
|---|---|---|
| email · phone · US-style SSN · credit-card number · **IPv4** · **IPv6** · **IBAN** | a person's **name** · **location** · nationality/religious/political group (**NRP**) | national identifiers (an Italian fiscal code, a passport number) · file paths |

## Presidio (opt-in NER supplement, ADR 0026 clause 1)

Names and places need named-entity recognition, which a regex structurally cannot do — this is
what **Presidio** adds, as a *supplement* to the regex detectors above, never a replacement for
them:

```bash
pip install 'examlops[guardrails-presidio]'   # presidio-analyzer + spaCy (no model)

# One-time model install — plain pip works; a `uv`-managed venv (no `pip` binary) needs the
# wheel URL directly (verified 2026-09-19; `python -m spacy download` silently no-ops there):
python -m spacy download en_core_web_lg   # plain venv
# or, inside a uv venv:
uv pip install "https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl"

export EXAMLOPS_GUARDRAIL_PII_NER=1   # off by default — see below for why
```

- **Additive, not a switch.** `detect_pii`/`redact_pii` always run the regex detectors; Presidio,
  when enabled, adds only the entity types a regex cannot find at all (`PERSON`, `LOCATION`,
  `NRP`). Presidio's own bundled recognizers for email/phone/SSN/credit-card/IBAN/IP are never
  used — the regex detectors above already own that ground, tested against real edge cases (a
  MAC address, a timecode, a documentation slug that looks like a credential). This is not a
  hypothetical caution: verified 2026-09-19 that Presidio's own `UsSsnRecognizer` **fails to
  match a plain `123-45-6789`** even at its lowest score threshold — a defect in Presidio itself,
  not a reason to trust it less than the fallback it would replace.
- **Off by default.** Presidio + a spaCy model is a real, if modest, dependency — the model is a
  separate download this extra deliberately does not pull in (spaCy models are not ordinary PyPI
  packages). A deployment that has not opted in gets exactly today's regex-only behaviour, at
  zero extra startup cost.
- **Model choice is an honest accuracy trade, not a default to fight.** `EXAMLOPS_GUARDRAIL_PRESIDIO_MODEL`
  (default `en_core_web_lg`, Presidio's own recommendation) selects the spaCy model. The small
  model (`en_core_web_sm`, ~13 MB) works and is what the opt-in test suite verifies against, but
  is measurably less accurate: verified 2026-09-19 that it mistagged a person's name and a street
  address as `ORGANIZATION` in the same sentence a `PERSON`/`LOCATION` pair it *did* get right
  moments earlier — spaCy's small model trades entity-type precision for size. Use the large
  model in production; the small one is for evaluating the feature without a 380 MB download.
- **Never breaks the guardrail.** Any failure to construct the engine (package missing, model not
  installed, a bad call) degrades to `None` — regex-only — logged once, never raised. `exa
  guardrails` and the gateway boundary behave identically whether or not Presidio is installed;
  enabling it only ever adds findings, never removes the regex ones.

`tests/unit/test_guardrails_ner.py` covers the wiring with a stub engine (always runs) and
re-verifies against the real library, `-m live`, skipped unless it is actually installed.

**Credentials are the secret scanner's half**, and the guardrail runs it on every input and output.
That half had a hole worth knowing about: until 2026-09-13 it recognised AWS keys, Slack tokens,
Fernet keys and private keys — **but not the credentials this platform itself issues**. The gateway
mints virtual keys as `exa-` + 32 random characters, so a key pasted into a prompt passed the
guardrail untouched and went on to the model provider, the cache and the logs. Upstream provider
keys (`sk-…`, `sk-ant-…`) and GitHub tokens were missed for the same reason, on a platform whose
whole job is proxying to those providers.

All four are detected now. The virtual-key rule is deliberately strict — a real key is exactly 32
characters and mixes case and digits — because a loose `exa-[\w-]{24,}` matched **462** ordinary
documentation slugs like `exa-status-platform-snapshot-at-a-glance`, and a scanner that fires on
the docs is a scanner someone switches off.

IPv6 and IBAN were added on 2026-09-13; before that a deployment on IPv6 had its addresses stored
in the clear while the IPv4 ones were redacted. IPv6 candidates are **confirmed** with
`ipaddress.IPv6Address` before anything is replaced, because a pattern loose enough to match every
IPv6 form also matches MAC addresses (`00:1b:44:11:3a:b7`) and timecodes (`01:02:03:04`) — and
redacting those as "ipv6" would put a wrong label on data that is not an address. One deliberate
gap: an address written with a **leading** `::` (such as `::1`) is not matched, because matching it
would also redact `abc::def`, which is valid C++ as well as a valid address, and prompts here carry
code. Loopback identifies nobody.

`tests/unit/test_guardrails.py` pins all three lists — detected, left alone, and knowingly
undetected — so the table above cannot quietly drift from the code.

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
