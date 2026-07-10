# Using kube-q (`kq`) as the ExaMLOps agent chat client

[kube-q](https://github.com/MSKazemi/kube_q) is a general-purpose terminal chat
client (`kq`) — session history, full-text search, conversation branching,
token/cost tracking, human-in-the-loop approvals, and rich rendering. It is used
**unforked, straight from PyPI**. ExaMLOps adapts *to* it by exposing an
OpenAI-compatible bridge on the agent server, so one `kq` binary can drive
ExaMLOps, KubeIntellect, or any other agentic backend by URL/profile.

```
kq  ──HTTP + SSE──▶  skipper /v1/chat/completions  ──▶  LangGraph + ExaMLOps tools
```

The bridge lives in `skipper/oai_compat.py` and is mounted by the agent server
(`make skipper-server`, port 18004). It implements exactly what kube-q's default
`kube-q` backend expects: `POST /v1/chat/completions` (SSE / JSON) and
`GET /healthz`, with conversation state keyed by the `X-Session-ID` header →
LangGraph `thread_id`.

## Quick start

```bash
# Terminal 1 — run the agent server (web UI + OpenAI/kube-q bridge)
make skipper-server

# Terminal 2 — chat via kq
make skipper-chat
#   ≡  kq --url http://localhost:18004
```

Single-shot / pipe-friendly:

```bash
kq --url http://localhost:18004 --query "which models are in production?" --output plain
```

## Human-in-the-loop

Write tools (delete/promote/retrain) trip a LangGraph `interrupt()`. `kq` shows an
approval panel and switches its prompt to `HITL>`. Type `/approve` to proceed or
`/deny` to cancel — these are relayed to the graph as `Command(resume=…)`.

## Authentication (optional)

Set `AGENT_API_KEY` on the server to require a bearer token; pass the same value
to `kq --api-key <key>` (or `KUBE_Q_API_KEY`). Unset ⇒ the bridge is open (local
dev default). `make skipper-chat` forwards `AGENT_API_KEY` automatically when set.

## Profile (optional convenience)

kube-q profiles are `.env` fragments under `~/.kube-q/profiles/`. Copy the
committed template so you can launch with a named profile:

```bash
mkdir -p ~/.kube-q/profiles
cp platform/services/agent/kube-q/examlops.profile.env ~/.kube-q/profiles/examlops.env
KUBE_Q_PROFILE=examlops kq          # or: kq --profile examlops
```

## Why not fork kube-q into this repo?

The chat client is domain-agnostic; only the tools/prompts are ExaMLOps-specific,
and those already live server-side. Forking into every agentic app creates N-way
drift — one client + one SSE contract per backend scales, N forks don't. See
`internal design notes` for the full rationale.
