from __future__ import annotations

import json
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

# Set by main.py callback flags
json_mode: bool = False  # True for any structured (non-table) format — gates existing branches
yes_mode: bool = False  # --yes/-y skips confirmation prompts
quiet_mode: bool = False  # --quiet/-q suppresses non-essential chatter (info/hint/detail)
verbose_mode: bool = False  # --verbose/-v enables extra detail() output
output_format: str = "table"  # one of: table | json | yaml | csv
# The last info() line suppressed in a structured mode — the fallback document carries it.
_last_info: str | None = None
# Documents buffered by print_json while the structured-output guard is installed.
_buffer: list[Any] | None = None


# ── Core output primitives ────────────────────────────────────────────────────


def print_json(data: Any) -> None:
    """Emit structured data in the active machine-readable format (json/yaml/csv/md/html).

    Named ``print_json`` for historical reasons — it now dispatches on ``output_format``.
    The whole CLI reaches structured output through this one function, so every format is
    uniform without touching individual commands (add a format here, all commands gain it).

    Under the structured-output guard (every real `exa` invocation in a structured mode) the
    document is buffered and emitted — merged with any others — when the command closes, so a
    command that calls ``ok()`` and then prints a record still prints one document.
    """
    if _buffer is not None:
        _buffer.append(data)
        return
    _emit(data)


def _emit(data: Any) -> None:
    if output_format == "yaml":
        typer.echo(_to_yaml(data))
    elif output_format == "csv":
        typer.echo(_to_csv(data))
    elif output_format == "md":
        typer.echo(_to_md(data))
    elif output_format == "html":
        typer.echo(_to_html(data))
    else:
        typer.echo(json.dumps(data, indent=2, default=str))


def _to_yaml(data: Any) -> str:
    try:
        import yaml

        return yaml.safe_dump(
            data, sort_keys=False, default_flow_style=False, allow_unicode=True
        ).rstrip("\n")
    except Exception:
        # Fall back to JSON if PyYAML is unavailable or the data isn't representable.
        return json.dumps(data, indent=2, default=str)


def _to_csv(data: Any) -> str:
    import csv
    import io

    buf = io.StringIO()
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        fields: list[str] = []
        for row in data:
            for k in row:
                if k not in fields:
                    fields.append(k)
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in data:
            writer.writerow({k: _scalar(v) for k, v in row.items()})
    elif isinstance(data, dict):
        row_writer = csv.writer(buf)
        row_writer.writerow(["key", "value"])
        for k, v in data.items():
            row_writer.writerow([k, _scalar(v)])
    else:
        # Not tabular — degrade to JSON so no data is silently lost.
        return json.dumps(data, default=str)
    return buf.getvalue().rstrip("\n")


def _scalar(value: Any) -> Any:
    """Flatten nested values so CSV cells stay single-line."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def _md_cell(value: Any) -> str:
    """Render a value for a Markdown table cell — flattened and pipe-escaped."""
    return str(_scalar(value)).replace("|", "\\|").replace("\n", " ")


def _to_md(data: Any) -> str:
    """Render structured data as a GitHub-flavoured Markdown table.

    List-of-dicts → a column table; a flat dict → a two-column Key/Value table; anything
    else degrades to a fenced JSON block so no data is silently lost (mirrors CSV).
    """
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        fields: list[str] = []
        for row in data:
            for k in row:
                if k not in fields:
                    fields.append(k)
        lines = ["| " + " | ".join(fields) + " |", "| " + " | ".join(["---"] * len(fields)) + " |"]
        for row in data:
            lines.append("| " + " | ".join(_md_cell(row.get(k, "")) for k in fields) + " |")
        return "\n".join(lines)
    if isinstance(data, dict):
        lines = ["| Key | Value |", "| --- | --- |"]
        for k, v in data.items():
            lines.append(f"| {_md_cell(k)} | {_md_cell(v)} |")
        return "\n".join(lines)
    return f"```json\n{json.dumps(data, indent=2, default=str)}\n```"


def _to_html(data: Any) -> str:
    """Render structured data as a self-contained HTML ``<table>`` (values HTML-escaped).

    List-of-dicts → a column table; a flat dict → a two-column table; anything else degrades
    to an escaped ``<pre>`` JSON block.
    """
    import html as _html

    def esc(v: Any) -> str:
        return _html.escape(str(_scalar(v)))

    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        fields = []
        for row in data:
            for k in row:
                if k not in fields:
                    fields.append(k)
        head = "".join(f"<th>{esc(k)}</th>" for k in fields)
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(row.get(k, ''))}</td>" for k in fields) + "</tr>"
            for row in data
        )
        return f"<table>\n<thead><tr>{head}</tr></thead>\n<tbody>{body}</tbody>\n</table>"
    if isinstance(data, dict):
        rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in data.items())
        return f"<table>\n<tbody>{rows}</tbody>\n</table>"
    return f"<pre>{esc(json.dumps(data, indent=2, default=str))}</pre>"


# Every message below is Rich-escaped. Callers pass plain prose, and prose contains brackets:
# `examlops[mcp]`, a TOML `[project.entry-points]` header, a `[WARNING]` log line. Unescaped,
# Rich reads those as style tags and *silently deletes them* — `pip install examlops[mcp]`
# renders as `pip install examlops`, an instruction that installs the wrong thing. A hint that
# quietly lies is worse than no hint, so the escaping is central rather than per-call-site.


def ok(message: str) -> None:
    if json_mode:
        print_json({"ok": True, "message": message})
    else:
        console.print(f"[green]✓[/green] {escape(message)}")


def error(message: str, exit_code: int = 1, hint: str | None = None) -> NoReturn:
    """Print an error and leave the command. Never returns — the ``raise`` below is
    unconditional, and saying so in the signature is what lets a caller's
    ``if x is None: _output.error(...)`` actually narrow ``x`` afterwards instead of
    every later use of it being read as possibly-None."""
    if json_mode:
        payload: dict[str, Any] = {"error": message, "exit_code": exit_code}
        if hint:
            payload["hint"] = hint
        print_json(payload)
    else:
        err_console.print(f"[red]✗[/red] {escape(message)}")
        if hint:
            err_console.print(f"[dim]  → {escape(hint)}[/dim]")
    raise typer.Exit(exit_code)


def warning(message: str) -> None:
    """Non-fatal warning — does not exit. Always on stderr: in a structured mode stdout carries
    exactly one document, and a warning printed there made it two."""
    if json_mode:
        err_console.print(json.dumps({"warning": message}), highlight=False, markup=False)
    else:
        err_console.print(f"[yellow]⚠[/yellow] {escape(message)}")


def info(message: str) -> None:
    """Informational message — skipped in JSON or quiet mode.

    In a structured mode the line is remembered: when a command's only output was an info
    line (typically "No X yet."), the end-of-command fallback reports it as one document
    instead of printing nothing (see :func:`install_structured_guard`).
    """
    global _last_info
    if json_mode:
        _last_info = message
        return
    if quiet_mode:
        return
    console.print(f"[dim]{escape(message)}[/dim]")


def hint(message: str) -> None:
    """Suggest a next action — skipped in JSON or quiet mode.

    The message is Rich-escaped so literal brackets (e.g. a TOML
    ``[project.entry-points."examlops.cli_plugins"]`` snippet) render verbatim
    instead of being silently eaten as an invalid markup tag.
    """
    if json_mode or quiet_mode:
        return
    console.print(f"[dim italic]  → {escape(message)}[/dim italic]")


def detail(message: str) -> None:
    """Extra diagnostic detail — shown only under --verbose (never in JSON/quiet)."""
    if json_mode or quiet_mode or not verbose_mode:
        return
    console.print(f"[dim]· {escape(message)}[/dim]")


# ── Structured output ─────────────────────────────────────────────────────────


def print_table(title: str, columns: list[str], rows: list[list[Any]]) -> None:
    if json_mode:
        print_json([dict(zip(columns, row)) for row in rows])
        return
    n = len(rows)
    caption = f"{n} item{'s' if n != 1 else ''}"
    table = Table(title=title, show_header=True, header_style="bold cyan", caption=caption)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(v) if v is not None else "—" for v in row])
    console.print(table)


def print_record(data: dict[str, Any]) -> None:
    if json_mode:
        print_json(data)
        return
    for k, v in data.items():
        console.print(f"  [bold cyan]{k}:[/bold cyan] {v}")


# ── The structured-output contract ────────────────────────────────────────────
# `--json` (and yaml/csv/md/html) promise exactly one document on stdout. Two mechanisms keep
# that true for every command without each one remembering to: a watch that emits a fallback
# document when a command printed nothing, and `run_external` for tools that write their own
# output.


class _StdoutWatch:
    """A transparent stdout proxy that notes whether anything visible was written."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.written = False

    def write(self, text: str) -> int:
        if text.strip():
            self.written = True
        return self._inner.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


_STATUS_KEYS = frozenset({"ok", "message", "error", "exit_code", "hint", "warning"})


def merge_documents(docs: list[Any]) -> Any:
    """Fold a command's structured documents into one.

    Status documents (``ok()``/``error()`` shapes) and data documents merge into one object —
    data keys win over status keys, later data over earlier. A single list of data stays a list
    when there is no status to carry, else it goes under ``items``. An error anywhere removes the
    ``ok: true`` an earlier ``ok()`` claimed.
    """
    if len(docs) == 1:
        return docs[0]
    status: dict[str, Any] = {}
    data: list[Any] = []
    for doc in docs:
        if isinstance(doc, dict) and doc and set(doc) <= _STATUS_KEYS:
            status.update(doc)
        else:
            data.append(doc)
    if "error" in status:
        status.pop("ok", None)
    if data and all(isinstance(d, dict) for d in data):
        merged: dict[str, Any] = dict(status)
        for d in data:
            merged.update(d)
        # An error raised after the data is the outcome; keep it visible over any data key.
        for key in ("error", "exit_code", "hint"):
            if key in status:
                merged[key] = status[key]
        return merged
    if not data:
        return status
    if len(data) == 1 and not status:
        return data[0]
    return {**status, "items": data[0] if len(data) == 1 else data}


def install_structured_guard(ctx: Any) -> None:
    """In a structured mode, make sure the command ends having printed exactly one document.

    Two failure modes, one mechanism. A command whose only output was a suppressed ``info()``
    line printed *nothing* — an empty stdout every JSON consumer rejects; now it reports
    ``{"ok": true, "message": <that line>}``. A command that called ``ok()`` and then printed
    its record printed *two* (``exa --json retrain`` printed three); now ``print_json`` buffers
    and the documents are merged (:func:`merge_documents`) when the command closes.
    """
    import sys

    global _last_info, _buffer
    _last_info = None
    _buffer = []
    watch = _StdoutWatch(sys.stdout)
    sys.stdout = watch

    def _close() -> None:
        global _buffer
        docs, _buffer = _buffer or [], None
        sys.stdout = watch._inner
        if docs:
            _emit(merge_documents(docs))
        elif not watch.written:
            _emit({"ok": True, "message": _last_info} if _last_info else {"ok": True})

    ctx.call_on_close(_close)


def run_external(
    cmd: list[str], *, not_found: str, failed: str, parse: Callable[[str], Any] | None = None
) -> None:
    """Run an external tool (docker compose, pytest, a generator script).

    In table mode its output streams to the terminal as before. In a structured mode it is
    captured and reported as one document — ``parse(stdout)`` when given (e.g. ``docker compose
    ps --format json``), else ``{"ok": true, "output": …}`` — because the tool's own text on
    stdout made `--json` output unparseable. ``failed`` may contain ``{code}``.
    """
    import subprocess

    if not json_mode:
        try:
            subprocess.run(cmd, check=True, text=True)  # noqa: S603
        except FileNotFoundError:
            error(not_found)
        except subprocess.CalledProcessError as exc:
            error(failed.format(code=exc.returncode))
        return
    try:
        done = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    except FileNotFoundError:
        error(not_found)
    if done.returncode != 0:
        tail = [ln for ln in (done.stderr or done.stdout).strip().splitlines() if ln.strip()]
        error(failed.format(code=done.returncode), hint=tail[-1][:300] if tail else None)
    print_json(parse(done.stdout) if parse else {"ok": True, "output": done.stdout})


# ── Interaction helpers ───────────────────────────────────────────────────────


def principal_kind() -> str:
    """Who drives this invocation — ``"agent"`` or ``"human"`` (ADR 0147 d2).

    An agent runtime marks its workers with ``EXAMLOPS_PRINCIPAL_KIND=agent``; anything else is a
    human. Stated limit: an agent that shells out with a human's environment is indistinguishable
    from that human — this marks agents that identify themselves, it does not detect the rest.
    """
    import os

    return (
        "agent" if os.getenv("EXAMLOPS_PRINCIPAL_KIND", "").strip().lower() == "agent" else "human"
    )


def confirm(prompt: str, default: bool = False, auto_yes: bool = False) -> bool:
    """Prompt for confirmation — auto-yes under ``--yes`` or structured output, for humans only.

    ``-o json`` is an output format, not consent. For a human's script it has always meant "don't
    prompt", and still does. An **agent** principal never gets implicit consent: it is refused
    with a structured ``plan_required`` error, because the consent an agent needs is an approved
    plan (plan/apply, ADR 0147 d2 — USAR I9), not a flag it can set on itself.

    ``auto_yes`` is for a command's *own* ``--yes``/``--force``-style option, which is distinct
    from the global ``--yes`` (``yes_mode``). Pass it here rather than skipping this call —
    several commands used to guard the call itself (``if not yes and not confirm(...)``), which
    skipped the agent check above along with the prompt whenever that local flag was set (ADR
    0147 d2 finding: an agent passing a command's own ``--yes`` slipped through ungated). This is
    the one place that decides, so a bypass flag can only shorten a human's prompt, never an
    agent's.
    """
    if principal_kind() == "agent":
        error(
            f"plan_required: refusing to auto-confirm for an agent principal — {prompt}",
            hint="agent mutations need an approved plan (plan/apply, ADR 0147); a human can run "
            "this command, or unset EXAMLOPS_PRINCIPAL_KIND for a human-driven script",
        )
    if auto_yes or yes_mode or json_mode:
        return True
    return typer.confirm(prompt, default=default)


# ── Progress ─────────────────────────────────────────────────────────────────


@contextmanager
def spinner(message: str) -> Generator[None, None, None]:
    """Display a Rich spinner while the block executes. No-op in JSON mode."""
    if json_mode:
        yield
        return
    with console.status(f"[bold blue]{message}[/bold blue]"):
        yield


# ── Live / watch ───────────────────────────────────────────────────────────


def watch_loop(render: Callable[[], None], interval: int) -> None:
    """Re-run ``render`` every ``interval`` seconds, clearing the screen between frames.

    Exits cleanly on Ctrl-C. No-op guard: callers should skip this in JSON mode.
    """
    import time

    interval = max(1, int(interval))
    try:
        while True:
            console.clear()
            console.print(
                f"[dim]● live — refreshing every {interval}s — press Ctrl-C to exit[/dim]"
            )
            render()
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped watching.[/dim]")


# ── Visual primitives (graphics) ─────────────────────────────────────────────

_SPARK_TICKS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float]) -> str:
    """Render a compact unicode sparkline from a numeric series (empty string if <2 points)."""
    nums = [float(v) for v in values if v is not None]
    if len(nums) < 2:
        return ""
    lo, hi = min(nums), max(nums)
    span = hi - lo
    if span == 0:
        return _SPARK_TICKS[0] * len(nums)
    out = []
    last = len(_SPARK_TICKS) - 1
    for n in nums:
        idx = int((n - lo) / span * last)
        out.append(_SPARK_TICKS[min(last, max(0, idx))])
    return "".join(out)


def bar(value: float, maximum: float, width: int = 20) -> str:
    """Render a horizontal bar (``value`` of ``maximum``) using block characters."""
    if maximum <= 0:
        return "░" * width
    frac = max(0.0, min(1.0, value / maximum))
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)
