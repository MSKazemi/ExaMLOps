# Tutorial — Skipper's Long-Term Memory

This tutorial walks through Skipper's cross-session memory (Phase 25): how to enable it,
teach the agent, verify what it learned, and administer/erase it. Everything runs
**self-hosted and offline** — no cloud embeddings.

> Background: `docs/guides/agent.md` (Long-Term Memory), ADR 0033 (architecture),
> ADR 0034 (governance).

## 1. What memory is for

Skipper already remembers each *conversation* (the LangGraph checkpointer). Long-term
memory is different — it persists **across** sessions and stores the agent's *experience*:

| Kind | Namespace | Example |
|---|---|---|
| **Procedural** | `proc` | "safe-promote: validate → drift check → canary 10% → promote" |
| **Episodic** | `episode` | "JPCP drift went CRITICAL — cause was a PM100 schema change; fixed by rebaseline" |
| **Preference** | `pref` | "alice always uses 5% canary" |
| **KB** | `kb` | "operators call `mbwidth` memory bandwidth" |

Memory holds experience + preferences + stable facts **only**. Current platform state
(versions, drift values, cost, approvals) is always queried live — never remembered —
because it changes constantly and the platform DB is the source of truth.

## 2. Enable it

Memory is on by default and additive. You only need an embedding backend:

```bash
# Option A — local Ollama (default). Start the tunnel and pull the model once:
ollama-tunnel start           # or a local `ollama serve`
ollama pull nomic-embed-text

# Option B — fully offline, in-process (no Ollama needed):
export AGENT_EMBED_BACKEND=sentence-transformers
export AGENT_EMBED_MODEL=all-MiniLM-L6-v2
export AGENT_EMBED_DIMS=384          # MUST match the model
```

If neither is available, the agent still runs — with short-term memory only (it logs a
warning). Start Skipper as usual: `make skipper`.

## 3. Teach it

In a chat session:

```
you> Remember that I always want a 5% canary before any promotion.
skipper> Noted preference for alice: canary percentage = 5%
```

After a successful multi-step operation, let Skipper capture it as a procedure. Because
a procedure write is **confirmation-gated**, you'll be asked to approve:

```
you> That promotion workflow worked well — save it as a procedure.
skipper> [confirm] record_procedure: Save a reusable procedure for 'safe-promote' (4 steps)
Proceed? [y/N] y
skipper> Recorded procedure for 'safe-promote' (4 steps).
```

## 4. See it recalled

In a **new** session, Skipper recalls what it learned before planning:

```
you> Promote MACK to production.
skipper> (recall_memory → found procedure 'safe-promote' + preference canary=5%)
         I'll follow your usual safe-promote flow with a 5% canary:
         1. validate_model_serving MACK  2. get_drift_status MACK
         3. serve traffic 5% canary      4. promote_model MACK
         Shall I start with the validation step?
```

Note: Skipper still calls the live tools to check MACK's *current* state — memory guides
the plan, the platform DB decides the facts.

## 5. Administer & erase (governance)

Every memory write is audited:

```bash
exa audit --source agent-memory --last 7d
```

Enumerate, export, and erase memory (deletions cascade and are audited; the audit log
itself is a separate store and is preserved):

```bash
exa agent memory stats                             # counts per kind
exa agent memory list proc                         # list procedures
exa agent memory export --out memory-backup.json   # GDPR export
exa agent memory delete pref --scope alice         # erase alice's preferences
# the same thing without the CLI, from platform/services/agent/:
make skipper-memory ARGS=stats
python -m skipper.memory_admin stats
```

## 6. Safety notes

- **Poisoned memory can't cause an unsafe action.** Memory tools only store/recall data;
  every dangerous tool (promote, retrain, restart, …) stays confirmation-gated regardless
  of memory content. A red-team test enforces this invariant.
- **Turn it off** anytime: `AGENT_MEMORY_ENABLED=false` (short-term memory only), or
  `AGENT_MEMORY_REQUIRE_CONFIRM=false` to let procedures save without a prompt (not
  recommended in shared/regulated environments).

## 7. Evaluating whether memory helps

Memory is evaluated on **task success**, not conversational recall: run a fixed
ops-scenario suite with memory on vs off and compare success rate, tool-call count,
clarifying questions, tokens, and latency (`skipper/memory_eval.py` holds the scenario
suite + the safety invariant; the full memory-on/off run needs a live LLM).

## 8. Beyond the four kinds — the full 7-tier stack

The procedural / episodic / preference / KB kinds above are tier **T1**. The next-gen memory
architecture (ADRs 0101/0104/0105/0106) adds:

- **T2 Knowledge / docs-RAG** — semantic search over the documentation (`search_knowledge`);
  build the index with `make skipper-knowledge-ingest`. **Check its exit code:** the tier needs a
  reachable embedding backend, and an ingest that indexed nothing exits `1` and names what was
  missing. Exit `0` with no files means only that you switched the tier off deliberately.
- **T3 Monitoring / baseline** — recall "what's normal" for a model (`recall_baseline`), auto-fed
  by the `skipper-watch` daemon (`make skipper-watch`).
- **T4 Outcome** — the reactive loop records tool telemetry so `tool_success_rate` is real.
- **T5 Consolidation** — `make skipper-consolidate` promotes recurring incidents into review-gated
  candidate procedures and deprecates procedures that rely on failing tools.
- **X Tenant scoping** — `AGENT_MEMORY_TENANT_SCOPED=true` isolates memory per project (opt-in).

See `docs/guides/agent.md` → *Next-gen architecture* for the full table.
