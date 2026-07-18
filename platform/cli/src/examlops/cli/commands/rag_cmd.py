"""B4 — `exa rag`: retrieval-augmented generation pipeline (ADR 0019)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from examlops.cli import _output

app = typer.Typer(
    help="RAG — ingest knowledge bases and query with citations",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa rag ingest kb1 --docs ./docs.jsonl --source-revision abc123\n\n"
    "  exa rag query kb1 --question 'how does promotion work?' -k 3\n\n"
    "  exa rag list"
)


@app.command("ingest", epilog=_EXAMPLES)
def ingest(
    kb: str = typer.Argument(..., help="Knowledge-base name"),
    docs: str = typer.Option(..., "--docs", help="JSONL of {id, text}"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace (D6)"),
    source_revision: str | None = typer.Option(
        None, "--source-revision", help="A1 dataset/source revision to version against"
    ),
) -> None:
    """Chunk, embed, and index documents into a knowledge base."""
    from examlops.rag import RagPipeline

    try:
        records = [json.loads(line) for line in Path(docs).read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        _output.error(f"Failed to load docs from {docs}: {exc}")
        return
    n = RagPipeline().ingest(kb, records, tenant=tenant, source_revision=source_revision)
    _output.ok(f"Ingested {len(records)} docs → {n} chunks into KB '{kb}' (tenant={tenant})")


@app.command("query", epilog=_EXAMPLES)
def query(
    kb: str = typer.Argument(..., help="Knowledge-base name"),
    question: str = typer.Option(..., "--question", help="The question to answer"),
    k: int = typer.Option(5, "-k", "--k", help="Number of chunks to retrieve"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
) -> None:
    """Answer a question from a knowledge base, citing retrieved chunks."""
    from examlops.rag import RagPipeline
    from examlops.vector_store import CollectionNotFound

    try:
        ans = RagPipeline().query(kb, question, tenant=tenant, k=k)
    except CollectionNotFound as exc:
        _output.error(str(exc))
        return
    if _output.json_mode:
        _output.print_json(
            {
                "answer": ans.answer,
                "citations": [{"doc_id": c.doc_id, "score": c.score} for c in ans.citations],
                "guardrail_flagged": ans.guardrail_flagged,
                "retrieval_span": ans.retrieval_span_id,
            }
        )
        return
    _output.info(f"Answer:\n{ans.answer}")
    if ans.guardrail_flagged:
        _output.warning(
            "D8 guardrail flagged injection content in a retrieved chunk (neutralized)."
        )
    _output.print_table(
        "Citations",
        ["Chunk", "Score"],
        [[c.doc_id, f"{c.score:.4f}"] for c in ans.citations],
    )


@app.command("list")
def list_kbs(
    tenant: str | None = typer.Option(None, "--tenant", help="Filter to one tenant"),
) -> None:
    """List knowledge bases and their versions."""
    from examlops.data import get_db, init_db

    init_db()
    q = "SELECT kb, tenant, source_revision, encoder, chunk_count, updated_at FROM rag_kbs"
    params: tuple = ()
    if tenant:
        q += " WHERE tenant=?"
        params = (tenant,)
    q += " ORDER BY updated_at DESC"
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No knowledge bases ingested.")
        return
    _output.print_table(
        "Knowledge Bases",
        ["KB", "Tenant", "Source rev", "Encoder", "Chunks"],
        [
            [
                r["kb"],
                r["tenant"],
                (r["source_revision"] or "—")[:12],
                r["encoder"],
                str(r["chunk_count"]),
            ]
            for r in rows
        ],
    )
