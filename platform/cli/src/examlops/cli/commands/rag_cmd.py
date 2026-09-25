"""B4 — `exa rag`: retrieval-augmented generation pipeline (ADR 0019)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from examlops.cli import _output
from examlops.cli._enums import FusionMethod

app = typer.Typer(
    help="RAG — ingest knowledge bases and query with citations",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa rag ingest kb1 --docs ./docs.jsonl --source-revision abc123\n\n"
    "  exa rag query kb1 --question 'how does promotion work?' -k 3\n\n"
    "  exa rag query kb1 --question 'why did job 4711 fail?' --retrieval hybrid\n\n"
    "  exa rag query kb1 --question 'how does promotion work?' --structured\n\n"
    "  exa rag ingest kb1 --docs ./docs.jsonl --framework llamaindex\n\n"
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
    framework: str | None = typer.Option(
        None,
        "--framework",
        help="Chunking framework: native | llamaindex | auto (default: EXAMLOPS_RAG_FRAMEWORK "
        "or native)",
    ),
) -> None:
    """Chunk, embed, and index documents into a knowledge base."""
    from examlops.rag import RagPipeline
    from examlops.rag.frameworks import FRAMEWORKS, RagFrameworkUnavailable

    try:
        records = [json.loads(line) for line in Path(docs).read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        _output.error(f"Failed to load docs from {docs}: {exc}")
        return
    if framework is not None and framework not in FRAMEWORKS:
        _output.error(f"--framework must be one of {', '.join(FRAMEWORKS)}")
        return
    try:
        n = RagPipeline(framework=framework).ingest(
            kb, records, tenant=tenant, source_revision=source_revision
        )
    except RagFrameworkUnavailable as exc:
        _output.error(str(exc))
        return
    _output.ok(f"Ingested {len(records)} docs → {n} chunks into KB '{kb}' (tenant={tenant})")


@app.command("query", epilog=_EXAMPLES)
def query(
    kb: str = typer.Argument(..., help="Knowledge-base name"),
    question: str = typer.Option(..., "--question", help="The question to answer"),
    k: int = typer.Option(5, "-k", "--k", help="Number of chunks to retrieve"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
    retrieval: str = typer.Option(
        "dense",
        "--retrieval",
        help="dense (embedding) | hybrid (embedding + BM25, rank-fused — finds exact ids/codes)",
    ),
    fusion: FusionMethod = typer.Option(
        FusionMethod.rrf, "--fusion", help="Hybrid fusion: rrf (default) | convex"
    ),
    structured: bool = typer.Option(
        False,
        "--structured",
        help="B8 structured answer: schema-valid JSON whose citations must name retrieved chunks",
    ),
) -> None:
    """Answer a question from a knowledge base, citing retrieved chunks."""
    from examlops.rag import RETRIEVAL_MODES, RagPipeline
    from examlops.structured import StructuredOutputError
    from examlops.vector_store import CollectionNotFound, EncoderMismatch

    if retrieval not in RETRIEVAL_MODES:
        _output.error(f"--retrieval must be one of {', '.join(RETRIEVAL_MODES)}")
    try:
        ans = RagPipeline(retrieval=retrieval, fusion=str(fusion)).query(
            kb, question, tenant=tenant, k=k, structured=structured
        )
    except (CollectionNotFound, EncoderMismatch, StructuredOutputError) as exc:
        _output.error(str(exc))
        return
    if _output.json_mode:
        doc = {
            "answer": ans.answer,
            "citations": [{"doc_id": c.doc_id, "score": c.score} for c in ans.citations],
            "guardrail_flagged": ans.guardrail_flagged,
            "retrieval_span": ans.retrieval_span_id,
        }
        if ans.structured is not None:
            doc["structured"] = ans.structured
            doc["grounded"] = ans.grounded
        _output.print_json(doc)
        return
    _output.info(f"Answer:\n{ans.answer}")
    if ans.structured is not None:
        cited = ", ".join(ans.structured.get("cited_chunks") or []) or "none"
        _output.info(f"Cited chunks: {cited}")
        if not ans.grounded:
            _output.warning("Answer is NOT grounded: it cites no retrieved chunk.")
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


_EXAMPLES_EVAL = (
    "Examples:\n\n"
    "  exa rag eval kb1 --items ./qa.jsonl                  # context precision/recall, persisted\n\n"
    "  exa rag eval kb1 --items ./qa.jsonl --backend ragas  # force the Ragas metrics\n\n"
    "  exa rag eval kb1 --items ./qa.jsonl --alias Production   # record the baseline\n\n"
    "  exa eval gate set rag:kb1 --suite rag-retrieval --metric context_recall:min=0.8"
    " --higher-is-better\n\n"
    "  exa --json rag eval kb1 --items ./qa.jsonl --retrieval hybrid -k 3"
)


@app.command("eval", epilog=_EXAMPLES_EVAL)
def eval_kb(
    kb: str = typer.Argument(..., help="Knowledge-base name"),
    items: str = typer.Option(
        ..., "--items", help="JSONL of {question, relevant_ids: [doc or doc#chunk ids]}"
    ),
    k: int = typer.Option(5, "-k", "--k", help="Chunks retrieved per question"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant namespace"),
    retrieval: str = typer.Option("dense", "--retrieval", help="dense | hybrid"),
    backend: str = typer.Option(
        "auto", "--backend", help="Metric backend: auto (ragas if installed) | ragas | native"
    ),
    suite: str = typer.Option("rag-retrieval", "--suite", help="C2 suite name to record under"),
    dataset_revision: str | None = typer.Option(
        None, "--dataset-revision", help="A1 revision of the question set"
    ),
    version: str | None = typer.Option(
        None, "--version", help="Candidate version for the C3 gate (default: KB source revision)"
    ),
    alias: str | None = typer.Option(
        None, "--alias", help="Record this run as a baseline alias (e.g. Production)"
    ),
    record: bool = typer.Option(
        True, "--record/--no-record", help="Persist to eval_suite_results (C2)"
    ),
) -> None:
    """Score a knowledge base's retrieval (context precision/recall) through the C2 harness."""
    from examlops.evaluation.rag_metrics import BACKENDS, RagEvalBackendUnavailable
    from examlops.rag import RETRIEVAL_MODES, RagPipeline
    from examlops.rag.evaluate import RagEvalInputError, evaluate_kb
    from examlops.vector_store import CollectionNotFound, EncoderMismatch

    if retrieval not in RETRIEVAL_MODES:
        _output.error(f"--retrieval must be one of {', '.join(RETRIEVAL_MODES)}")
        return
    if backend not in BACKENDS:
        _output.error(f"--backend must be one of {', '.join(BACKENDS)}")
        return
    try:
        qa = [json.loads(line) for line in Path(items).read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        _output.error(f"Failed to load items from {items}: {exc}")
        return
    try:
        result = evaluate_kb(
            kb,
            qa,
            pipeline=RagPipeline(retrieval=retrieval),
            tenant=tenant,
            k=k,
            backend=backend,
            suite=suite,
            dataset_revision=dataset_revision,
            version=version,
            alias=alias,
            persist=record,
        )
    except (
        RagEvalInputError,
        RagEvalBackendUnavailable,
        CollectionNotFound,
        EncoderMismatch,
    ) as exc:
        _output.error(str(exc))
        return
    if _output.json_mode:
        _output.print_json({**result, "recorded": record})
        return
    _output.print_table(
        f"RAG eval: {kb} · backend={result['backend']} (n={result['sample_size']})",
        ["Metric", "Score"],
        [[m, f"{v:.4f}"] for m, v in result["scores"].items()],
    )
    _output.ok(
        f"Recorded as suite '{suite}' for model '{result['model']}'"
        if record
        else "Not recorded (--no-record)"
    )
