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

## Governance

Every label move and rollback writes an `audit_events` row (D4). A label move may be
gated by a C3 eval check before it is allowed to point at `prod`.
