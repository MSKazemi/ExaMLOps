"""Offline reporting — cost / carbon / SLA / project summaries (Phase 3 item 3.5).

Assembles platform data (FinOps cost, Green-AI carbon, projects, SLOs) into a shareable report,
rendered to **HTML** (always — dependency-free), **PDF** (via WeasyPrint when installed), or plain
**text**. Everything degrades gracefully: a missing WeasyPrint falls back to HTML with a note; each
data section is fail-open so one empty/broken source never blanks the whole report.

`exa report generate [--format html|pdf|text] [--project NAME] [--out FILE]`.
"""

from __future__ import annotations

import html
from typing import Any


def assemble_report(
    *, project: str | None = None, generated_at: str | None = None
) -> dict[str, Any]:
    """Gather the report data model. ``generated_at`` is injected (CLI stamps it; scripts pass it)."""
    from examlops.data import init_db
    from examlops.data.finops import aggregate_model_costs, get_carbon_records
    from examlops.data.projects import get_project_consumption, list_projects

    init_db()
    report: dict[str, Any] = {
        "title": f"ExaMLOps Report — {project}" if project else "ExaMLOps Platform Report",
        "generated_at": generated_at,
        "scope": project or "platform",
        "sections": {},
    }

    # ── Cost ────────────────────────────────────────────────────────────────
    try:
        costs = aggregate_model_costs()
        report["sections"]["cost"] = {
            "rows": costs,
            "total_gpu_hours": round(sum(c["gpu_hours"] for c in costs), 2),
            "total_cost_usd": round(sum(c["cost_usd"] for c in costs), 2),
        }
    except Exception as exc:  # noqa: BLE001 - fail-open per section
        report["sections"]["cost"] = {"error": str(exc), "rows": []}

    # ── Carbon (Green-AI) ───────────────────────────────────────────────────
    try:
        records = get_carbon_records()
        total_g = sum((r.get("co2e_g") or 0) for r in records)
        report["sections"]["carbon"] = {
            "records": len(records),
            "total_kg_co2e": round(total_g / 1000.0, 3),
        }
    except Exception as exc:  # noqa: BLE001
        report["sections"]["carbon"] = {"error": str(exc), "records": 0}

    # ── Projects ────────────────────────────────────────────────────────────
    try:
        if project:
            projs = [{"name": project, "consumption": get_project_consumption(project)}]
        else:
            projs = [
                {"name": p["name"], "consumption": get_project_consumption(p["name"])}
                for p in list_projects()
            ]
        report["sections"]["projects"] = {"rows": projs}
    except Exception as exc:  # noqa: BLE001
        report["sections"]["projects"] = {"error": str(exc), "rows": []}

    return report


# ── renderers ────────────────────────────────────────────────────────────────────────────────


def render_text(report: dict[str, Any]) -> str:
    lines = [report["title"], "=" * len(report["title"])]
    if report.get("generated_at"):
        lines.append(f"Generated: {report['generated_at']}")
    lines.append(f"Scope: {report['scope']}\n")

    cost = report["sections"].get("cost", {})
    lines.append("COST")
    lines.append(f"  total GPU-hours: {cost.get('total_gpu_hours', 0)}")
    lines.append(f"  total cost USD:  {cost.get('total_cost_usd', 0)}")
    for r in cost.get("rows", [])[:20]:
        lines.append(
            f"    {r['model_name']}: {r['gpu_hours']} gpu-h, ${r['cost_usd']} ({r['runs']} runs)"
        )

    carbon = report["sections"].get("carbon", {})
    lines.append("\nCARBON (Green-AI)")
    lines.append(
        f"  total kg CO2e: {carbon.get('total_kg_co2e', 0)} over {carbon.get('records', 0)} records"
    )

    projects = report["sections"].get("projects", {})
    lines.append("\nPROJECTS")
    for p in projects.get("rows", [])[:20]:
        lines.append(f"    {p['name']}: {p.get('consumption', {})}")
    return "\n".join(lines) + "\n"


def render_html(report: dict[str, Any]) -> str:
    def esc(x: Any) -> str:
        return html.escape(str(x))

    cost = report["sections"].get("cost", {})
    carbon = report["sections"].get("carbon", {})
    projects = report["sections"].get("projects", {})

    cost_rows = "".join(
        f"<tr><td>{esc(r['model_name'])}</td><td>{esc(r['gpu_hours'])}</td>"
        f"<td>${esc(r['cost_usd'])}</td><td>{esc(r['runs'])}</td></tr>"
        for r in cost.get("rows", [])
    )
    proj_rows = "".join(
        f"<tr><td>{esc(p['name'])}</td><td>{esc(p.get('consumption', {}))}</td></tr>"
        for p in projects.get("rows", [])
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{esc(report["title"])}</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:2rem;color:#111}}
 h1{{border-bottom:2px solid #333}} table{{border-collapse:collapse;margin:.5rem 0}}
 td,th{{border:1px solid #ccc;padding:.3rem .6rem;text-align:left}}
 .kpi{{display:inline-block;margin-right:2rem;font-size:1.2rem}}
</style></head><body>
<h1>{esc(report["title"])}</h1>
<p>Scope: <b>{esc(report["scope"])}</b>{" · Generated: " + esc(report["generated_at"]) if report.get("generated_at") else ""}</p>
<h2>Cost</h2>
<div class="kpi">GPU-hours: <b>{esc(cost.get("total_gpu_hours", 0))}</b></div>
<div class="kpi">Total: <b>${esc(cost.get("total_cost_usd", 0))}</b></div>
<table><tr><th>Model</th><th>GPU-h</th><th>Cost</th><th>Runs</th></tr>{cost_rows}</table>
<h2>Carbon (Green-AI)</h2>
<p><b>{esc(carbon.get("total_kg_co2e", 0))}</b> kg CO2e over {esc(carbon.get("records", 0))} records</p>
<h2>Projects</h2>
<table><tr><th>Project</th><th>Consumption</th></tr>{proj_rows}</table>
</body></html>"""


def _weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def generate(
    fmt: str = "html",
    *,
    out: str | None = None,
    project: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble + render a report. Returns ``{format, path?, content?, degraded?}``.

    ``pdf`` degrades to ``html`` (with a flag) when WeasyPrint is unavailable, so a report is always
    produced. Writes to ``out`` when given, else returns the content inline.
    """
    report = assemble_report(project=project, generated_at=generated_at)
    fmt = fmt.lower()
    degraded = False

    if fmt == "text":
        content = render_text(report)
    elif fmt == "pdf":
        html_content = render_html(report)
        if _weasyprint_available():
            import weasyprint

            pdf_path = out or "report.pdf"
            weasyprint.HTML(string=html_content).write_pdf(pdf_path)
            return {"format": "pdf", "path": pdf_path, "degraded": False}
        # Degrade to HTML.
        degraded, content, fmt = True, html_content, "html"
    else:  # html (default / unknown)
        content, fmt = render_html(report), "html"

    if out:
        from pathlib import Path

        Path(out).write_text(content)
        return {"format": fmt, "path": out, "degraded": degraded}
    return {"format": fmt, "content": content, "degraded": degraded}
