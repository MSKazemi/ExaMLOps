from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

import typer

from examlops.cli import _output
from examlops.cli._config import load_config
from examlops.platform_db import get_db, init_db

app = typer.Typer(
    help="Feature importance explanations (XAI)",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_EXPLAIN = (
    "Examples:\n\n"
    "  # Get feature importance for JPCP Production model\n"
    "  exa serve explain explain JPCP\n\n"
    "  # Explain with a specific input payload, top 5 features\n"
    "  exa serve explain explain JPCP --input-json '{\"embedding\":[0.1]}' --top-n 5\n\n"
    "  # Use Staging alias\n"
    "  exa serve explain explain JPCP --alias Staging"
)

_EXAMPLES_HISTORY = (
    "Examples:\n\n  exa serve explain history JPCP\n\n  exa --json serve explain history JPCP"
)


@app.command("explain", epilog=_EXAMPLES_EXPLAIN)
def explain(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    input_json: str | None = typer.Option(
        None, "--input-json", "-i", help="JSON-encoded input dict (default: empty dict)"
    ),
    top_n: int = typer.Option(10, "--top-n", "-n", help="Number of top features to show"),
    alias: str = typer.Option("Production", "--alias", "-a", help="MLflow alias to explain"),
) -> None:
    """Request feature-importance scores from the Ray Serve explain endpoint."""
    cfg = load_config()
    init_db()

    input_data = json.loads(input_json) if input_json else {}
    input_hash = hashlib.md5((input_json or "{}").encode(), usedforsecurity=False).hexdigest()[:16]

    body = json.dumps({"alias": alias, "input": input_data}).encode()
    url = f"{cfg.ray_serve_url}/explain/{model}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    status = "ok"
    error_msg: str | None = None
    features: list[dict] = []

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        features = data.get("features", [])
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _output.warning(
                "Explain endpoint not available — SHAP not installed in serving container"
            )
            status = "unavailable"
            error_msg = "HTTP 404"
        else:
            status = "error"
            error_msg = f"HTTP {exc.code}"
            _output.error(f"Explain request failed: HTTP {exc.code}")
    except urllib.error.URLError as exc:
        status = "error"
        error_msg = str(exc.reason)
        _output.error(f"Explain request failed: {exc.reason}")

    # Log the request to DB
    with get_db() as conn:
        conn.execute(
            "INSERT INTO explain_logs (model, alias, input_hash, top_n, status, error) "
            "VALUES (?,?,?,?,?,?)",
            (model, alias, input_hash, top_n, status, error_msg),
        )

    if status != "ok":
        return

    # Sort by absolute importance and take top_n
    sorted_features = sorted(features, key=lambda f: abs(f.get("importance", 0.0)), reverse=True)
    top_features = sorted_features[:top_n]

    if _output.json_mode:
        _output.print_json({"model": model, "alias": alias, "features": top_features})
        return

    if not top_features:
        _output.ok(f"No feature importance data returned for {model} ({alias})")
        return

    rows = [[f.get("name", "—"), f"{f.get('importance', 0.0):.6f}"] for f in top_features]
    _output.print_table(f"Feature Importance — {model} ({alias})", ["Feature", "Importance"], rows)


@app.command("history", epilog=_EXAMPLES_HISTORY)
def explain_history(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
) -> None:
    """Show recent explain requests for a model."""
    init_db()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT ts, model, alias, top_n, status, error "
            "FROM explain_logs WHERE model=? ORDER BY ts DESC LIMIT 20",
            (model,),
        ).fetchall()

    if _output.json_mode:
        _output.print_json([dict(r) for r in rows])
        return

    if not rows:
        _output.ok(f"No explain history for {model}")
        return

    table_rows = [
        [
            (r["ts"] or "—")[:19],
            r["model"],
            r["alias"] or "—",
            str(r["top_n"] or "—"),
            r["status"] or "—",
            r["error"] or "—",
        ]
        for r in rows
    ]
    _output.print_table(
        f"Explain History — {model}",
        ["Time", "Model", "Alias", "Top N", "Status", "Error"],
        table_rows,
    )
