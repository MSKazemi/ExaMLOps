# Prompt management & versioning

ExaMLOps has a **prompt registry** with immutable versions and moving labels, so you
can change what a model is asked *without a code deploy* — and roll back instantly.
Prompts resolve by `name@label` (`triage@prod`), moves are audited, and the resolved
version attaches to GenAI traces (C1) and eval results (C2).

Design: ADR 0009 · spec `design/vision/specs/B1-prompt-management.md`. Backend is
`platform_db` (works with no external service); an MLflow Prompt Registry backend can
be swapped behind the same client.

## Concepts

- **Version** — immutable `{name, version, template, variables, actor, created_at}`.
  Editing a prompt always creates a **new** version; old ones never change.
- **Label** — a moving pointer (`dev`/`staging`/`prod`/…) to a specific version.

## CLI

```bash
exa prompt create triage --template "Classify: {text}" --label prod  # v1 + point prod at it
exa prompt create triage --template "Categorize: {text}"             # v2 (prod still v1)
exa prompt list                                                      # all prompt names
exa prompt list triage                                               # versions + which labels point where
exa prompt show triage@prod                                          # resolve a label to its template
exa prompt diff triage 1 2                                           # line diff between versions
exa prompt label triage prod 2                                       # move prod → v2 (audited)
exa prompt rollback triage prod 1                                    # roll prod back to v1 (history kept)
```

## Runtime resolution

```python
from examlops import prompts

pv = prompts.get_prompt("triage", "prod")          # cached; fails safe to last-known-good
text = prompts.render(pv, text=ticket_body)        # validates vars, raises before any model call
```

`render()` raises `ValueError` naming any **missing variable before** the model is
called (spec R2/R3), and substitutes variable values strictly as data — a value
containing `{...}` is never re-interpreted as a placeholder (spec R12; injection
defense is delegated to D8 guardrails).

`get_prompt()` caches with a short TTL and **fails safe to the last-known-good
version** if the registry is unreachable (spec R6), so a registry blip can't take down
inference.

### The two built-in consumers

A registry with no reader changes nothing — prompts stay in code and a change still needs a
deploy. Both of the consumers ADR 0009 names now resolve through `get_prompt()`:

**Skipper** resolves `skipper-system@<label>` at graph build time. The literal in
`skipper/prompts.py` stays as the seed and the fail-safe, so an absent, unreachable or empty
registry never stops the agent from starting.

```bash
python -c "from skipper.prompts import seed_system_prompt; seed_system_prompt()"  # idempotent
exa prompt label skipper-system prod --version 2     # change the agent, no deploy
exa prompt rollback skipper-system prod              # and back
```

`SKIPPER_PROMPT_REGISTRY=0` pins the literal; `SKIPPER_PROMPT_LABEL` picks the label.

**The model gateway** takes a `prompt_ref` per request and prepends the resolved template as a
system message:

```python
client.chat("gpt-4o", [{"role": "user", "content": q}], prompt_ref="support-bot@prod")
```

The caller's own messages are never rewritten — the registry prompt goes *in front of* any
system message they sent, and their list is not mutated. The version that served the request
is recorded on the C1 span as `examlops.prompt.version`.

**One deliberate asymmetry.** Skipper falls back to its literal; the gateway does **not** — an
unresolvable `prompt_ref` raises `LookupError` before any backend is called. Skipper has a
literal that is always a correct system prompt. A caller who asked for `support-bot@prod` has
no such default, and serving the request without it would change the model's behaviour
invisibly. Failing loudly is the only honest option.

## Governance

Every label move and rollback writes an `audit_events` row (D4).

### The C3 regression gate on a label move

Moving a label to `prod` changes what every caller gets, so it is gated by the same C3 eval
regression check that gates a model promotion. **Nothing happens until you configure a gate** —
an unconfigured prompt moves exactly as it always did.

```bash
# 1. Configure the gate. The subject is `prompt:<name>`, never the bare name.
exa eval gate set prompt:triage --suite quality --metric accuracy:max_drop=0.01 --baseline prod

# 2. Score the candidate version and the baseline into that suite.
#    (model_version = the prompt version; alias = the baseline label)

# 3. A regressing version is now refused, and the refusal is audited.
exa prompt label triage prod 2
# ✗ Eval gate FAILED for triage v2: accuracy. Use --force to override (audited).

exa prompt label triage prod 2 --force     # proceeds, writes an eval_gate_override event
```

Three things worth knowing, each of which is silent if you get it wrong:

- **The subject is `prompt:<name>`.** `eval_gates` is one keyspace shared with models. A prompt
  named `jpcp` configured as a bare `jpcp` would be judged against the *model* jpcp's scores —
  a gate firing on the wrong evidence, which is worse than no gate.
- **Only `prod` is gated**, configurable with `EXAMLOPS_PROMPT_GATE_LABELS` (comma-separated).
  `dev` and `staging` are deliberately open: the gate reads its baseline from a *labelled*
  version, so gating them would leave no way to establish the baseline the gate needs.
- **`exa prompt rollback` is never gated.** It is the remedy when a live prompt is bad — which
  is exactly when its scores are failing — so gating it would trap you on the version you are
  trying to escape.
