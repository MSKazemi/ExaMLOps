---
title: Follow an LLM request
description: How ExaMLOps serves large language models — a chat request through the model gateway's key, guardrail, cache, routing and cost steps, the vLLM endpoint lifecycle on Compose, HPC and Kubernetes, and retrieval-augmented answers.
hide:
  - navigation
  - toc
---

# Follow an LLM request

Large language models run on the same platform as the rest of ExaMLOps, with one extra piece
in front of them: the **model gateway**. A request through it can be checked against a
virtual key and its budget, is scanned by a guardrail, can be answered from a cache, and is
routed to a model server and costed. The model servers themselves are vLLM processes that
`exa serve llm` starts on Compose or a Slurm or Flux allocation, or registers when someone else
runs them; for Kubernetes it prepares and validates a KServe manifest that you apply.

The gateway is a library that runs inside the calling process. There is no separate gateway
service to deploy; whatever calls it — an `exa` command or your own code — applies the same
rules.

## A request through the gateway

<div class="xm-player" data-scene="llm" markdown>
<ol class="xm-steps">
<li data-focus="c-cli,c-rag,c-judge,c-code,key" data-run="cli-key,rag-key,judge-key,code-key" data-actor="Callers" data-line="data"><strong>Four callers use the gateway.</strong> <code>exa gateway chat</code>, the answer step of <code>exa rag query</code>, the judge in <code>exa serve challenger judge</code>, and Python code that builds a <code>GatewayClient</code>. Only callers that pass a virtual key get the key and budget check; the RAG and judge calls do not pass one. The Skipper agent calls its own configured model backend directly — Ollama when no hosted backend is set — and does not go through the gateway.</li>
<li data-focus="key" data-run="cli-key" data-actor="Gateway" data-line="control"><strong>The virtual key is checked first.</strong> Before any model is called: an unknown or revoked key, a model missing from the key's allow-list, or spend already at the budget each stop the request with a typed error. <code>exa gateway key issue</code> prints a key once; only its hash is stored, and issuing and revoking are audited. A request with no key skips this check.</li>
<li data-focus="prompt" data-run="key-prompt" data-actor="Gateway" data-line="control"><strong>A caller can name a registry prompt.</strong> With <code>prompt_ref="support-bot@prod"</code> the gateway prepends that prompt version as a system message and records the version on the trace, so changing the prompt is a label move, not a redeploy. A reference that does not resolve is an error. The template is trusted, versioned text, so the guardrail scans only the caller's own messages.</li>
<li data-focus="guardin" data-run="prompt-guardin" data-actor="Guardrail" data-line="control"><strong>The request is scanned.</strong> In the default <code>monitor</code> mode the guardrail records what it finds in each message's text — prompt injection, personal data, secrets — and changes nothing. That includes the text parts of a message sent as a list of content parts, such as an image with a question; image parts are left to the media guard. In <code>enforce</code> mode it blocks injection and redacts personal data and secrets. A blocked request never reaches the cache or a model. Set the mode with <code>EXAMLOPS_GUARDRAIL_MODE</code>.</li>
<li data-focus="cache,reply" data-run="guardin-cache;cache-reply" data-actor="Semantic cache" data-line="data"><strong>A close match can be answered from the cache.</strong> When the cache is on, a request whose last message embeds at least 0.85 similar to a stored one — for the same model and the tenant the cache was set up for, stored within the last hour — gets the stored answer without a model call. The system prompt, earlier turns and the caller's sampling settings are not part of the match, and a request containing content parts never reads or writes the cache. The cache lives in the calling process, so a single <code>exa gateway chat --cache</code> call starts empty; a long-running caller benefits.</li>
<li data-focus="router,registry" data-run="registry-router,cache-router" data-actor="Router" data-line="control"><strong>The router picks a model.</strong> Each route name maps to backends in priority order, and an error moves to the next. The default table holds an echo route named <code>default</code> and one route for every endpoint registered with <code>exa serve llm start</code>, under the endpoint's name. No echo stands in for a down endpoint: the request fails and says which endpoint failed.</li>
<li data-focus="media,vllm" data-run="router-media;media-vllm" data-actor="Engine" data-line="hpc"><strong>Images are checked, then the server answers.</strong> Image parts are validated before the request leaves — the per-prompt image limit, the domain allow-list, the local-path root, and a size cap (20 MiB by default) on inline and local images — and a rejected image is never retried elsewhere. The vLLM server receives the chat request under the name of the weights it serves.</li>
<li data-focus="meter,tempo" data-run="vllm-meter;meter-tempo" data-actor="Gateway" data-line="observe"><strong>The call is costed.</strong> Cost comes from the <code>llm_cost</code> provider when an operator has selected one, else the built-in rate table, else a flat rate. Each call writes a <code>gateway_calls</code> row and adds its cost to the key's spend, which the next budget check reads. With tracing on, a GenAI span carries the model, tenant, tokens and a rate-table cost estimate to Tempo.</li>
<li data-focus="guardout" data-run="meter-guardout" data-actor="Guardrail" data-line="control"><strong>The answer is scanned on the way out.</strong> In <code>enforce</code> mode toxic output is blocked and personal data and secrets are redacted. The cost was recorded first on purpose: the tokens were spent whatever the guardrail decides, so the bill matches the provider's.</li>
<li data-focus="schema" data-run="guardout-schema" data-actor="Gateway" data-line="control"><strong>A schema, if asked for, is enforced.</strong> With <code>response_schema</code> the gateway finds the JSON in the reply, validates it, and repairs it once by default — locally, by dropping unknown fields, coercing types and filling missing required fields with empty values, not by asking the model again. If it still does not validate, the caller gets <code>StructuredOutputError</code>. This validates after generation; it does not constrain the model while it generates. A cache hit is validated the same way; a stored answer that does not fit the schema counts as a miss, and the model is asked.</li>
<li data-focus="reply,cache" data-run="schema-cache,schema-reply" data-actor="Gateway" data-line="data"><strong>The answer is stored and returned.</strong> A blocked or schema-invalid answer is never cached. The caller receives the text, the backend that answered — <code>endpoint:qwen</code>, <code>echo</code> or <code>cache</code> — the token counts and the cost.</li>
</ol>
</div>

### What can stop a request

| Stage | Stops the request when | Error |
|---|---|---|
| Virtual key | The key is unknown or revoked · the model is not on its allow-list · spend has reached the budget | `KeyInvalid` · `ModelNotAllowed` · `BudgetExceeded` |
| Registry prompt | The named prompt or label does not exist | the registry's lookup error |
| Guardrail, inbound | `enforce` mode finds prompt injection, or the scanner itself fails | `GuardrailBlocked` |
| Media guard | An image breaks the endpoint's limit, allow-list, path root or size cap | `MediaNotAllowed` |
| Router | Every backend for the route failed, or no route has that name | `AllBackendsFailed` |
| Guardrail, outbound | `enforce` mode finds toxic output | `GuardrailBlocked` |
| Structured output | The reply cannot be made to fit the schema | `StructuredOutputError` |

`exa gateway chat` prints the error name and reason. `exa rag query` does not surface them: its
answer step reports "(no answer)" instead.

## Run a model server

<div class="xm-player" data-scene="llmserve" markdown>
<ol class="xm-steps">
<li data-focus="op,start,engine" data-run="op-start,engine-start" data-actor="Operator" data-line="human"><strong>An operator starts an endpoint.</strong> <code>exa serve llm start qwen</code> previews with <code>--dry-run</code>, asks for confirmation and writes an audit event. The model's <code>engine:</code> block, overridden by flags such as <code>--tp</code> and <code>--max-model-len</code>, is rendered into <code>vllm serve</code> flags by one function that the Compose service, the Slurm job and the KServe manifest all use. A vision model must set <code>--max-images</code>.</li>
<li data-focus="ext,compose,kserve,hpcjob" data-run="start-ext,start-compose,start-kserve,start-hpc" data-actor="Launcher" data-line="hpc"><strong>One of four launchers takes it.</strong> <code>external</code> (the default) registers a server someone else runs. <code>compose</code> starts the GPU <code>vllm</code> service. <code>kserve</code> builds a Kubernetes manifest and checks it with a server-side dry run, but never applies it — you apply it with <code>kubectl</code>. <code>slurm</code> and <code>flux</code> submit a batch job through the scheduler the launcher is named after; the same job script detects whether it runs under Slurm or Flux and places each step through it.</li>
<li data-focus="vllm" data-run="ext-vllm,compose-vllm,kserve-vllm,hpc-vllm" data-actor="Launcher" data-line="hpc"><strong>A vLLM server comes up.</strong> On an HPC allocation, Apptainer runs the vLLM image; across several nodes a Ray cluster forms, and <code>--tp</code> and <code>--pp</code> set tensor parallelism inside a node and pipeline parallelism across nodes. The job is recorded in the HPC job table as a serving job, so <code>exa hpc jobs</code> lists it.</li>
<li data-focus="registry" data-run="start-registry" data-actor="Registry" data-line="control"><strong>The endpoint is recorded.</strong> The registry keeps its address, state, launcher, job id, project, modality and engine block. An external endpoint is READY at once; Compose and HPC endpoints start as STARTING while the model loads, and a KServe endpoint starts as PENDING.</li>
<li data-focus="hpcjob,epfile,registry" data-run="hpc-epfile;epfile-registry" data-actor="HPC job" data-line="hpc"><strong>An HPC job publishes its own address.</strong> The job writes its URL to an endpoint file as soon as its head node is known; starting and stopping the endpoint remove any file a previous job left. <code>exa serve llm health</code>, <code>status</code> and <code>chat</code> read that file, or fetch it over SSH, and record the address; a gateway request picks it up only when the file is visible on the machine it runs on. The work directory must be on a filesystem the compute nodes share with the login node.</li>
<li data-focus="vllm,health,registry" data-run="vllm-health;health-registry" data-actor="Operator" data-line="observe"><strong>A health probe marks it ready.</strong> <code>exa serve llm health qwen</code> probes <code>/health</code>, lists the models the server serves, and records READY or FAILED; it exits 1 when the endpoint is not ready, so it works as a deploy gate.</li>
<li data-focus="registry,gateway,prom" data-run="registry-gateway,registry-prom" data-actor="Registry" data-line="control"><strong>The endpoint is now a gateway route and a metrics target.</strong> The gateway routes to it by name, with keys, budgets, guardrails, caching and cost applied. Prometheus scrapes the Compose server directly. <code>exa hpc prometheus-sd</code> writes a target for every other endpoint that is READY or STARTING with a recorded, non-loopback address — re-run it when that changes. Alerts cover a down endpoint, a nearly full KV cache, a queue backlog and slow first tokens.</li>
</ol>
</div>

### Launchers at a glance

| | External | Compose | Slurm / Flux | KServe |
|---|---|---|---|---|
| Starts | Nothing — registers a URL | The `vllm` service (GPU profile) | A batch job: Apptainer, Ray across nodes | Nothing — builds and dry-runs an `LLMInferenceService` |
| Address | The `--base-url` you give | `http://localhost:18011` | Published by the job, read on first use | `EXAMLOPS_KSERVE_GATEWAY_URL` |
| State after start | READY | STARTING | STARTING | PENDING |
| `exa serve llm stop` | Marks it STOPPED; nothing is killed | `docker compose stop vllm` | Cancels the job (`scancel` / `flux cancel`) | Refuses: delete it with `kubectl` |
| Works without a GPU | Yes | No | No | Manifest only |

## Answer from your documents

<div class="xm-player" data-scene="rag" markdown>
<ol class="xm-steps">
<li data-focus="docs,chunk" data-run="docs-chunk" data-actor="Operator" data-line="human"><strong>Documents are chunked.</strong> <code>exa rag ingest runbooks --docs runbooks.jsonl</code> reads one JSON object per line, each with an id and its text, and splits the text into 40-word windows that overlap by 10 words, so a phrase of up to ten words cut at a boundary is whole in the next window.</li>
<li data-focus="embed,vstore" data-run="chunk-embed;embed-vstore" data-actor="Ingest" data-line="data"><strong>Chunks are embedded and stored.</strong> By default the embedding is a deterministic 64-dimension token hash, so ingest needs no model and no service. Chunks go into the vector store — tables in the platform datastore by default, or pgvector — and the knowledge base records its source revision, its encoder and its chunk count.</li>
<li data-focus="ask,qembed" data-run="ask-qembed,embed-qembed" data-actor="Operator" data-line="human"><strong>A question is embedded the same way.</strong> <code>exa rag query runbooks --question "How do I drain a node?"</code> embeds the question with the same function used at ingest, so the question and the chunks are compared in one space. The pipeline's encoder is checked against the one stamped on the knowledge base, and a mismatch is refused rather than scored.</li>
<li data-focus="search,vstore" data-run="qembed-search,vstore-search" data-actor="Query" data-line="data"><strong>The nearest chunks are retrieved.</strong> The store returns up to three times as many candidates as the answer will use, for the reranker to choose from.</li>
<li data-focus="rerank" data-run="search-rerank" data-actor="Query" data-line="data"><strong>They are reranked.</strong> Candidates are ordered by how many of the question's words they contain, and the top <code>k</code> (5 by default) are kept. With tracing on, a retriever span records the ids and scores of the chunks kept.</li>
<li data-focus="guard" data-run="rerank-guard" data-actor="Guardrail" data-line="control"><strong>Retrieved text is treated as untrusted.</strong> A chunk that says "ignore previous instructions" is an attack on the model, not a fact about your system. Such phrases are replaced with <code>[redacted-instruction]</code>, the chunk stays in the context as data, and the answer is flagged.</li>
<li data-focus="assemble,gateway,answer" data-run="guard-assemble;assemble-gateway;gateway-answer" data-actor="Gateway" data-line="control"><strong>The prompt goes through the gateway.</strong> The chunks are numbered into a prompt that asks for an answer from the context alone, with chunk citations, and sent to the gateway's <code>default</code> route — the request path above, without a virtual key. The echo route answers unless a model endpoint is registered as <code>default</code> — and an echo returns the whole assembled prompt as the answer. If the gateway call fails for any reason, the answer reads "(no answer)" rather than showing the error. The citations list the <code>k</code> chunks retrieved, whether or not the answer used them.</li>
</ol>
</div>

!!! note "Not built yet"
    The gateway validates structured output after generation; constraining generation to a
    schema is on the [roadmap](roadmap.md). The semantic cache and the default RAG embedding
    are in-process fallbacks — the cache does not persist between processes, and the token-hash
    embedding matches shared words rather than meaning. The SGLang engine is a stub until a GPU
    host exists, and no real GPU has run this path yet: it is tested against a stub vLLM server
    over real sockets. The Flux launch path is tested by running the rendered job script under
    stand-in Slurm and Flux commands, not yet on a Flux instance with GPUs, and KServe endpoints are never applied for you.

## Try it

```bash
# Register a server someone else runs, check it, and route through the gateway
exa serve llm start qwen --base-url http://gpu01:8000 --hf-model Qwen/Qwen3-8B
exa serve llm health qwen
KEY=$(exa --json gateway key issue --tenant acme --project chat --model qwen --budget 20 \
      | jq -r .virtual_key)
exa gateway chat qwen --message "Summarise this alert" --key "$KEY"
exa gateway key list

# Or launch one on a cluster (preview first)
exa serve llm start qwen --launcher slurm --nodes 2 --gpus 4 --tp 4 --pp 2 \
    --hf-model Qwen/Qwen3-8B --dry-run

# Retrieval-augmented answers
exa rag ingest runbooks --docs runbooks.jsonl
exa rag query runbooks --question "How do I drain a node?"
exa guardrails stats
```

## Read more

- [Model gateway](../guides/model-gateway.md) — routes, keys, cost, prompts, schemas, caching
- [Serving LLMs and VLMs](../guides/vlm-serving.md) — launchers, multimodal safety, HPC
- [Guardrails](../guides/guardrails.md) and [prompt management](../guides/prompt-management.md)
- [RAG](../guides/rag.md) and [GenAI observability](../guides/genai-observability.md)
- [Judge calibration](../guides/judge-calibration.md) — before an LLM judge may gate anything
