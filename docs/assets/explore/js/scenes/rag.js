/* Answer from your documents: `exa rag ingest` and `exa rag query`. Sources: examlops/rag
 * (RagPipeline.ingest/query, chunk_text, default_embed, lexical_reranker, the injection seam),
 * examlops/vector_store (select_store, the encoder guard) and cli/commands/rag_cmd.py. */
XM.register("rag", {
  title: "Answer from your documents",
  description: "Ingest splits documents into overlapping chunks, embeds them and stores them in a vector store under the knowledge base. A query embeds the question the same way, retrieves the nearest chunks, reranks them by word overlap, neutralises instructions hidden in retrieved text, assembles a prompt and sends it through the model gateway, returning the answer with the chunks that were retrieved.",
  viewBox: [0, 20, 1400, 500],
  zones: [
    { x: 14, y: 40, w: 780, h: 170, label: "Ingest", line: "data" },
    { x: 14, y: 250, w: 1372, h: 260, label: "Query", line: "data" }
  ],
  nodes: [
    { id: "docs", x: 120, y: 130, label: "exa rag ingest", sub: "JSONL of id and text", line: "human",
      info: { title: "Ingest documents", tasks: ["One JSON object per line: an id and the text", "--source-revision versions the knowledge base against a dataset revision", "--tenant keeps knowledge bases apart"], cli: ["exa rag ingest runbooks --docs runbooks.jsonl", "exa rag list"] } },
    { id: "chunk", x: 310, y: 130, label: "Chunk", sub: "40-word windows, 10 overlap", line: "data",
      info: { title: "Chunking", tasks: ["Windows of 40 words that overlap by 10, so a phrase of up to ten words cut at a boundary is whole in the next window", "Each chunk is stored as <doc id>#<n>, the id an answer lists"] } },
    { id: "embed", x: 510, y: 130, label: "Embed", sub: "64-dim token hash", line: "data",
      info: { title: "Embedding", tasks: ["By default a deterministic 64-dimension token-hash vector: no model, no service", "The knowledge base records which encoder built it"] } },
    { id: "vstore", x: 710, y: 130, label: "Vector store", sub: "SQLite · pgvector", kind: "store", line: "data",
      info: { title: "Vector store", sub: "EXAMLOPS_VECTOR_BACKEND", tasks: ["sqlite (default): tables in the platform datastore", "pgvector: a Postgres extension", "Each collection is stamped with the encoder that built it; a search that names another encoder is refused"], cli: ["exa vector stats runbooks"] } },

    { id: "ask", x: 120, y: 350, label: "exa rag query", sub: "--question · -k 5", line: "human",
      info: { title: "Ask a question", tasks: ["Prints the answer, the k retrieved chunks with their scores, and a warning if the guardrail fired", "With only the echo route, the answer is the assembled prompt itself"], cli: ["exa rag query runbooks --question \"How do I drain a node?\""] } },
    { id: "qembed", x: 310, y: 350, label: "Embed question", sub: "same function as ingest", line: "data" },
    { id: "search", x: 500, y: 350, label: "Search", sub: "3 × k nearest", line: "data",
      info: { title: "Retrieve", tasks: ["Asks the store for up to three times as many chunks as needed, for the reranker to choose from"] } },
    { id: "rerank", x: 690, y: 350, label: "Rerank", sub: "word overlap → top k", line: "data",
      info: { title: "Rerank", tasks: ["Orders the candidates by how many of the question's words each contains", "Keeps the top k (5 by default)", "With tracing on, a retriever span records the chunks kept", "The reranker is pluggable"] } },
    { id: "guard", x: 880, y: 350, label: "Guardrail", sub: "retrieved text", line: "control",
      info: { title: "Retrieved text is untrusted", tasks: ["A chunk that says \"ignore previous instructions\" is an attack on the model, not a fact", "Such phrases are replaced with [redacted-instruction] and the answer is flagged", "The chunk stays in the context as data"], links: [{ text: "Guardrails", href: "guides/guardrails.md" }] } },
    { id: "assemble", x: 1070, y: 350, label: "Prompt", sub: "context + question", line: "data",
      info: { title: "Assemble the prompt", tasks: ["Numbers each chunk and asks for an answer that uses only the context and cites chunk numbers"] } },
    { id: "gateway", x: 1260, y: 350, label: "Gateway", sub: "default route", line: "control",
      info: { title: "Generate through the gateway", tasks: ["Goes to the gateway's default route, with its guardrail scan and cost record, but no virtual key", "Answered by the echo route unless a model endpoint is registered as default", "Any gateway error is shown as (no answer)"], links: [{ text: "Follow an LLM request", href: "explore/llm.md" }] } },
    { id: "answer", x: 1260, y: 460, label: "Answer", sub: "with retrieved chunk ids", line: "data" }
  ],
  edges: [
    { id: "docs-chunk", from: "docs", to: "chunk", line: "data" },
    { id: "chunk-embed", from: "chunk", to: "embed", line: "data" },
    { id: "embed-vstore", from: "embed", to: "vstore", line: "data", label: "upsert" },
    { id: "embed-qembed", from: "embed", to: "qembed", line: "data", dashed: true, start: [490, 157], via: [[490, 195], [330, 195]], end: [330, 323], label: "same embedding", labelAt: 0.45 },
    { id: "ask-qembed", from: "ask", to: "qembed", line: "human" },
    { id: "qembed-search", from: "qembed", to: "search", line: "data" },
    { id: "vstore-search", from: "vstore", to: "search", line: "data", start: [690, 161], via: [[690, 230], [520, 230]], end: [520, 323], label: "nearest chunks", labelAt: 0.4 },
    { id: "search-rerank", from: "search", to: "rerank", line: "data" },
    { id: "rerank-guard", from: "rerank", to: "guard", line: "data" },
    { id: "guard-assemble", from: "guard", to: "assemble", line: "data" },
    { id: "assemble-gateway", from: "assemble", to: "gateway", line: "control" },
    { id: "gateway-answer", from: "gateway", to: "answer", line: "data" }
  ]
});
