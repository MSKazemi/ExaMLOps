# Honest degradation

ExaMLOps degrades rather than failing, in a lot of places and on purpose. A broken plugin must not
block a promotion. A serving replica must keep answering when the datastore is away. A wall display
must never show an error page. None of that is in question here.

What this page is about is the **second half of that promise**, which the platform got wrong on six
separate surfaces before it was named: when a component degrades, the result must be
*distinguishable from the ordinary case*. A fallback nobody can tell from normal operation is not
resilience — it is a silent policy change, and it survives precisely because everything looks fine.

Every instance below was found in the same week, in code that passed its tests.

## What the defect looks like

| Surface | It degraded to | Which was indistinguishable from |
|---|---|---|
| Governance → *Audit integrity* | a hash chain it recomputed itself, `verified: true` | genuine tamper-evidence — but the digest was one no other tool could reproduce, and "verified" was true *by construction* |
| `exa finetune`, reproducibility bundles | `(None, None)` for the signature | "this site configured no signing key" — so a malformed key or a down secret store read as deliberate policy |
| Provider resolution (drift, promotion, placement, carbon, cost) | the built-in default | "no provider is configured" — so a site's stricter promotion gate could be replaced without a word |
| Serving gateway | an unchanged route table | a correctly-empty registry — the symptom was a configured model answering *unknown model* |
| `exa connection test` | probing without the secret | wrong credentials — a diagnosis tool misdiagnosing |
| NOC wall | `—` *"Awaiting data"* | a source that simply had not reported yet |
| Next-Gen console | `0` on every tile | an idle platform with nothing configured |
| Compliance, Fairness, SLO and Drift reads | `[]` | an empty register — so "no model is in scope of the EU AI Act" was a sentence a failed query could write |

The shape repeats: **the fallback was right and the silence was the defect.**

The last row is the sharpest case of rule 1, because the claim is regulatory rather than
operational. Six reads wrapped their query in `except Exception: return []`, and the console draws
an empty list the same way whether the register is empty or unreadable. They now answer `503`
naming the surface (`readfail.readable()`), which the console renders as an error with a retry. See
[Governance & Compliance](dashboard-governance.md#an-empty-register-and-an-unreadable-one-are-different-answers).

## The rules

**1. A zero is a claim; a dash is the absence of one.** `0 device pools` asserts that none exist. If
you are showing it because a request failed, you have made a claim you did not earn. Render `—`, and
say why.

**2. Separate "not yet" from "cannot".** They resolve differently: one waits, the other is someone's
job. The NOC wall distinguishes *Awaiting data* from *`<source>` source unavailable* for exactly this
reason — on a display read from across a room, silence reads as quiet.

**3. Degrade quietly only for the cause you documented.** Signing degrades silently when a site
configured no key, because that is a choice. Every other cause — a malformed key, an unreachable
store, a bug in the signer — is logged with its cause and says, in the message, that it is *not* the
same as having no key. One shared helper does this so the distinction cannot be fixed in one caller
and left broken in another.

**4. A gate carries the reason in its verdict, not only in the logs.** Promotion is not a
calculation; somebody reviews its record later. So when a configured provider fails, the fallback
cause is appended to the verdict itself:

```
0.0420 < 0.05 [built-in threshold used: configured provider failed — ValueError: coefficient table is empty]
```

**5. Silence is allowed when it is argued.** Two sites on the serving request path stay quiet
deliberately: they run per inference, and a log line each would drown the outage that caused them.
That argument lives in the code, on the `except` line.

**6. A first page is not an answer.** This is the same defect with no `except` in sight, so none of
the rules above catch it: a paginated API returns a page plus a `next_page_token`, the caller reads
the page, and a partial list is indistinguishable from a complete one. Nothing failed, nothing was
caught, and every test passes — because a fake that returns everything at once cannot tell a caller
that follows the token from one that ignores it.

It is worse than a short list whenever something is *selected* out of the result, because then the
answer flips rather than shrinks:

| Read | Stopping at page one produced |
|---|---|
| Dashboard model versions | the Production alias resolved from a partial list → **"no production version"** for a model that is serving one |
| `examlops.sdk.status()` | models filtered to those holding a lifecycle alias → **nothing in production**, to a script with nothing to check it against |
| Agent `list_models` | a name matched against one page → **"No models found in the registry"** for a model that exists |
| Agent platform summary | `len(page)` → **"Registered models: 100"**, forever |
| MCP `list_models` | the same partial registry, to a caller least able to notice |
| `exa models list` | "all registered models" — the command you use to find out whether a model exists |
| `exa models rollback` | rollback candidates capped at one page — on the one command whose purpose is to reach *backwards* |
| Dataplane snapshots | committed revisions filtered out of a page of *pulls* → **no snapshots** for a source whose recent pulls failed |

So: **follow the token, and refuse rather than truncate.** Every one of these now loops until the
token is absent, and stops with an error — not a short list — if a registry repeats a token or never
stops paging. `examlops.serving_snapshot._registered_models` had carried this loop, and the comment
naming it *"the 100-model bug, by design"*, since long before the others were found; the fix was
already written down in the codebase, it just had not been applied where it was needed.

The registry callers now share one implementation, `examlops.mlflow_paging.all_items`, because
seven independent rediscoveries of the same bug is what a missing helper looks like. It takes the
caller's own transport (a `fetch(url) -> dict`), so `urllib`, `httpx` and the agent's request
wrapper all use it without agreeing on anything else.

Note how the sweep that found the last four had to work. Searching for `next_page_token` or
`max_results` only finds callers that *already* mention paging — and a caller that has the bug
most completely mentions neither. The search that works starts from the **endpoint**: find every
call to an API known to paginate, then ask which of them handle continuation.

The test fakes page too, at a deliberately tiny page size. A fake that hands back everything in one
response is not a simplification — it is the assumption under test, and it makes the bug invisible
in exactly the place that is supposed to find it.

**7. "The latest" needs something that can actually order.** `CURRENT_TIMESTAMP` has **one-second
resolution**. On a table that keeps every write — `model_cards`, `ab_tests`, `hpo_studies`, each a
new `id` per row — two writes in one second is not a rare race; it is what a CI job or a
regenerate-all loop does. `ORDER BY ts DESC LIMIT 1` then returns not the latest row but whichever
of the tied rows the query plan reaches first, and SQLite reaches the **oldest** first.

Like rule 6, there is no `except` here and nothing fails. Three model cards written in one second,
and the dashboard served the first as *the current card* — an EU AI Act artefact, stale, and stable
enough to look right every time you check. The same tie picked which A/B test an analysis reported
on.

`LIMIT N` is only safe while the caller *renders* the rows. `rows[0]` turns it back into "the
latest record" with the tie still unresolved — a `LIMIT 1` wearing a `LIMIT N`, which a guard
looking for `LIMIT 1` cannot see. A second guard tracks that shape and currently stands at **zero**;
the case that motivated it is `get_gate_reports`, a `LIMIT N` list whose first entry the MCP
`gate_reports` tool hands an agent as the standing verdict.

Add the tiebreaker that carries the real sequence: `ORDER BY ts DESC, id DESC`. The distinction that
keeps the guard quiet is between **append-only** tables (`id INTEGER PRIMARY KEY AUTOINCREMENT`,
many rows per key) and **one row per key** tables (`traffic_rules`, `shadow_config`:
`model TEXT PRIMARY KEY`) — there is nothing to tie in the second kind, and demanding a tiebreaker
there would be noise. The guard reads the schema to tell them apart rather than carrying a list of
the files that were fixed, and it checks its own classifier against both kinds first.

## What enforces it

| Mechanism | Holds |
|---|---|
| [`test_degradations_are_visible.py`](testing.md#degradations-must-be-distinguishable) | a blanket `except` inside a function whose docstring promises a degradation must log, return the cause, or carry its reason. A **ratchet** — the count may only fall (8 → 4 so far) |
| `test_provider_fallback_is_visible.py` | every resolver reports a failed provider, and promotion's verdict says it fell back |
| `test_adapter_signing_degrades_honestly.py` | a missing key stays quiet, a signing failure does not |
| `test_governance.py` | the audit card reports the log's own chain, and a broken tail reads as broken |
| `NextGen.test.tsx`, `noc.test.ts` | dashes rather than zeros, and "unavailable" rather than "awaiting" |
| `test_failed_reads_are_not_empty_reads.py` | no dashboard router gains a silent `except Exception: return []`, and the six governance reads hold none. A **ratchet**, counted per file by an AST walk |
| `test_empty_is_not_all_clear.py`, `Compliance.unavailable.test.tsx` | a broken datastore gives 503 on both layers, *and* an empty schema still gives `200 []` |
| `test_latest_row_is_unambiguous.py` | no append-only table picks "the latest" row by a one-second timestamp alone (rule 7) |

## How to find the next one

Not by reading the code. Measuring this class with `grep` produced three different wrong answers in
one sitting — 27 pages, then 3, then 1 — because the same intent is written in different words.

**Execute the degraded path and read what the user is shown.** For a page, mount it with a rejecting
fetch. For a command, point it at a dead datastore and look at stdout, stderr and the exit code. For
a library, make the dependency raise and inspect the return value. Two lines:

```tsx
apiFetch.mockImplementation(() => Promise.reject(new Error('upstream 503')))
expect(container.textContent).not.toMatch(/0Device pools/)
```

Surfaces checked this way and found **honest**, recorded so the ground is not re-covered: every `exa`
read command (exit 2, a stderr warning naming the address, no stdout) and the documented exit-code
gates (`exa data checkout`, `exa hpc preflight`); `exa status`, which reports an unreachable control
plane with a suggested fix; the agent's tools, whose two error paths return the cause to the model;
every swallowing `catch` in the frontend, each carrying its reason; and the five consoles that render
a *Partial data* pill from the BFF's `_partial` list.

!!! warning "That frontend sweep was narrower than the sentence suggests — corrected 2026-09-14"
    "Every swallowing `catch` in the frontend" was true and still is, but a `catch` is not the only
    way a page turns a failure into an empty screen. The Compliance page held no `catch` at all: it
    wrote `const { data: systems = [] } = useComplianceSystems()` and rendered *No systems in the
    register* whenever the query rejected. **The default value is the swallow**, and it is invisible
    to any search for `catch`. When auditing a page, read what it does with the query's `error`, not
    only what it does with its exceptions — and note that fixing the backend alone would have changed
    nothing an operator sees, because the false all-clear was being produced independently at both
    layers.

## Related

- [Testing strategy](testing.md#degradations-must-be-distinguishable) — the ratchet and its rules
- [Supply-chain security](supply-chain-security.md) — the two causes of "unsigned"
- [FinOps providers](finops-providers.md) — what happens when a configured provider fails
- [Dashboard frontend engineering](dashboard-frontend-engineering.md) — a failed load must not look
  like an empty platform
- [Game days](game-days.md) — the drills that break dependencies on purpose
