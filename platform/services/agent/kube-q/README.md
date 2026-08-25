# Optional kube-q (`kq`) compatibility

The canonical first-party terminal client is `exa chat`:

```bash
make skipper-server
exa chat
exa chat --session incident-42
```

It provides the ExaMLOps-specific interactive workflow: `/help`, `/status`, `/sessions`,
`/history`, `/new`, `/resume ID`, `/approve`, `/deny`, and `/quit`.

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
(`make skipper-server`, port 18004). It implements `POST /v1/chat/completions`
(SSE or JSON) and `GET /healthz`, with conversation state keyed by the
verified bearer principal, server-configured tenant, and client `X-Session-ID` label. A caller
cannot enumerate or resume another principal's LangGraph thread.

This is deliberately a limited, read-only compatibility surface. Basic chat, streaming,
and session IDs work; kube-q features that call other backend endpoints
or inspect Kubernetes context do not. In particular, do not assume commands such
as findings, digest, replay, postmortem, detector, preferences, namespaces, or
Kubernetes context are provided by Skipper.

## Quick start

```bash
# Terminal 1 — run the agent server (web UI + OpenAI-compatible bridge)
make skipper-server

# Terminal 2 — optional third-party client
kq --url http://localhost:18004
```

Single-shot / pipe-friendly:

```bash
kq --url http://localhost:18004 --query "which models are in production?" --output plain
```

## Write actions and approval

Use the first-party `exa chat` or `exa ask` client for write proposals. ExaMLOps approvals require
an opaque, expiring action ID and a typed `approve` or `deny` decision; conversational text cannot
authorize a mutation. The generic kube-q protocol does not carry this ExaMLOps extension, so do not
enable write tools for kube-q sessions.

## Authentication (optional)

Set `AGENT_API_KEY` on the server to require a bearer token; pass the same value
to `kq --api-key <key>` (or `KUBE_Q_API_KEY`). Unset ⇒ the bridge is open (local
dev default).

## Profile (optional convenience)

kube-q profiles are `.env` fragments under `~/.kube-q/profiles/`. Copy the
committed template so you can launch with a named profile:

```bash
mkdir -p ~/.kube-q/profiles
cp platform/services/agent/kube-q/examlops.profile.env ~/.kube-q/profiles/examlops.env
KUBE_Q_PROFILE=examlops kq          # or: kq --profile examlops
```

## Why keep the compatibility bridge?

The chat client is domain-agnostic; only the tools/prompts are ExaMLOps-specific,
and those already live server-side. Keeping the standard endpoint lets external
clients integrate without making their command set part of the native ExaMLOps
CLI contract.
