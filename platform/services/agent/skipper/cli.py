from __future__ import annotations

import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from skipper import config
from skipper.confirm import _is_affirmative
from skipper.genai_trace import traced
from skipper.graph import build_graph
from skipper.llm import _FIX_HINT, check_backend

# ── Feature 7: ANSI colours (disabled for non-tty or NO_COLOR) ────────────────


class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    """Wrap text in ANSI code + reset, or return plain text when color is off."""
    return f"{code}{text}{_C.RESET}" if _USE_COLOR else text


# ── Feature 5: Token / cost tracking ─────────────────────────────────────────

_COST_PER_1M: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}

_COMMANDS = {
    "/help": "Show this help",
    "/tools": "List available tools",
    "/new": "Start a fresh conversation thread",
    "/resume <id>": "Resume a saved thread",
    "/threads": "List saved thread ids",
    "/history [n]": "Show last n messages in current thread (default 10)",  # Feature 1
    "/export [file]": "Export current thread to a Markdown file",  # Feature 2
    "/grep <pattern>": "Search conversation history for text",  # Feature 3
    "/watch <secs> <query>": "Repeat a query every N seconds (Ctrl+C stop)",  # Feature 4
    "/model <name>": "Switch the LLM model (rebuilds graph immediately)",
    "/report": "Generate a comprehensive platform status report",
    "/exit": "Quit",
}

_REPORT_PROMPT = (
    "Generate a comprehensive platform status report. Include: "
    "(1) executive summary of overall health, "
    "(2) production models with current metrics, drift scores, and any anomalies, "
    "(3) pending approval queue with recommendations, "
    "(4) recent training runs and outcomes, "
    "(5) top 3 issues requiring operator attention with suggested actions. "
    "Format as a structured Markdown report."
)


@dataclass
class CliState:
    thread_id: str = field(default_factory=lambda: f"cli-{uuid.uuid4().hex[:8]}")
    model: str = field(
        default_factory=lambda: (
            config.AZURE_OPENAI_DEPLOYMENT
            if config.AZURE_OPENAI_API_KEY and config.AZURE_OPENAI_ENDPOINT
            else config.ANTHROPIC_MODEL
            if config.ANTHROPIC_API_KEY
            else config.AGENT_MODEL
        )
    )


def format_interrupt(payload: dict) -> str:
    return f"[confirm] {payload.get('action')}: {payload.get('summary', '')}"


def _extract_text(content) -> str:
    """Extract visible text from message content, filtering out thinking blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            if isinstance(block, dict) and block.get("type") == "text"
            else (block if isinstance(block, str) else "")
            for block in content
        )
    return ""


# ── Feature 1: /history helper ────────────────────────────────────────────────


def _print_history(messages: list, n: int, thread_id: str) -> None:
    shown = messages[-n:]
    print(f"Thread {thread_id}  ({len(messages)} total, showing last {len(shown)}):")
    for msg in shown:
        role = type(msg).__name__.replace("Message", "").lower()
        name = getattr(msg, "name", None)
        if role == "tool" and name:
            role = f"tool:{name}"
        content = _extract_text(msg.content) if hasattr(msg, "content") else ""
        truncated = content[:100] + ("…" if len(content) > 100 else "")
        print(f"  [{role}] {truncated}")


# ── Feature 2: /export helper ─────────────────────────────────────────────────


def _export_thread(messages: list, path: str, thread_id: str) -> None:
    lines = [f"# Skipper thread ({thread_id})\n\n"]
    for msg in messages:
        role = type(msg).__name__.replace("Message", "")
        name = getattr(msg, "name", None)
        content = (
            _extract_text(msg.content)
            if hasattr(msg, "content")
            else str(getattr(msg, "content", ""))
        )
        header = f"**{role}**" + (f" `{name}`" if name else "")
        lines.append(f"{header}\n\n{content}\n\n---\n\n")
    Path(path).write_text("".join(lines))
    print(f"Exported {len(messages)} messages → {path}")


def handle_slash(text: str, state: CliState, graph=None) -> tuple[bool, object]:
    """Handle a slash-command. Returns (was_command, graph).

    The graph may be replaced on /model switch; callers must update their reference.
    """
    if not text.startswith("/"):
        return False, graph
    parts = text.split(maxsplit=1)
    cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    cfg = {"configurable": {"thread_id": state.thread_id}}

    if cmd == "/help":
        for name, desc in _COMMANDS.items():
            print(f"  {name:<30} {desc}")

    elif cmd == "/tools":
        from skipper.tools import TOOLS

        for t in TOOLS:
            print(f"  {t.name}")

    elif cmd == "/new":
        state.thread_id = f"cli-{uuid.uuid4().hex[:8]}"
        print(f"Started new thread: {state.thread_id}")

    elif cmd == "/resume":
        if arg:
            state.thread_id = arg.strip()
            print(f"Resumed thread: {state.thread_id}")
        else:
            print("Usage: /resume <id>")

    elif cmd == "/threads":
        if graph is not None:
            seen = {c.config["configurable"]["thread_id"] for c in graph.checkpointer.list(None)}
            print("\n".join(sorted(seen)) or "(no saved threads)")

    # Feature 1: /history [n]
    elif cmd == "/history":
        n = int(arg.strip()) if arg.strip().isdigit() else 10
        if graph is not None:
            try:
                messages = graph.get_state(cfg).values.get("messages", [])
            except Exception:
                messages = []
            _print_history(messages, n, state.thread_id)
        else:
            print("(no conversation yet)")

    # Feature 2: /export [file]
    elif cmd == "/export":
        path = arg.strip() if arg.strip() else f"exa-{state.thread_id}.md"
        if graph is not None:
            try:
                messages = graph.get_state(cfg).values.get("messages", [])
            except Exception:
                messages = []
            _export_thread(messages, path, state.thread_id)
        else:
            print("(no conversation yet)")

    # Feature 3: /grep <pattern>
    elif cmd == "/grep":
        if not arg.strip():
            print("Usage: /grep <pattern>")
        elif graph is not None:
            import re

            pattern = arg.strip()
            try:
                messages = graph.get_state(cfg).values.get("messages", [])
            except Exception:
                messages = []
            hits = 0
            for i, msg in enumerate(messages):
                role = type(msg).__name__.replace("Message", "").lower()
                content = _extract_text(msg.content) if hasattr(msg, "content") else ""
                if re.search(pattern, content, re.IGNORECASE):
                    hits += 1
                    print(f"  [{i}:{role}] {content[:120]}")
            if not hits:
                print(f"No matches for '{pattern}'")
        else:
            print("(no conversation yet)")

    # Feature 4: /watch <secs> <query>
    elif cmd == "/watch":
        watch_parts = arg.split(maxsplit=1)
        if len(watch_parts) < 2 or not watch_parts[0].isdigit():
            print("Usage: /watch <seconds> <query>")
        elif graph is not None:
            interval = int(watch_parts[0])
            query = watch_parts[1]
            print(f"Watching every {interval}s  (Ctrl+C to stop)")
            try:
                while True:
                    run_turn(graph, state, query)
                    time.sleep(interval)
            except KeyboardInterrupt:
                print("\nWatch stopped.")
        else:
            print("(no graph — start a conversation first)")

    elif cmd == "/model":
        if arg:
            state.model = arg.strip()
            graph = build_graph(model=state.model)
            print(f"Model switched to {state.model}")
        else:
            print(f"Current model: {state.model}  (usage: /model <name>)")

    elif cmd == "/report":
        if graph is not None:
            run_turn(graph, state, _REPORT_PROMPT)

    elif cmd in ("/exit", "/quit"):
        raise SystemExit(0)

    else:
        print(f"Unknown command: {cmd}")

    return True, graph


def run_turn(graph, state: CliState, user_input: str) -> None:
    cfg = {"configurable": {"thread_id": state.thread_id}}
    inp: object = {"messages": [HumanMessage(content=user_input)]}
    last_usage: dict = {}

    while True:
        in_ai_block = False

        try:
            for item in graph.stream(inp, traced(cfg), stream_mode="messages"):
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                msg, _meta = item
                if isinstance(msg, AIMessageChunk):
                    # Feature 5: capture token usage from final chunk
                    if getattr(msg, "usage_metadata", None):
                        last_usage = dict(msg.usage_metadata or {})
                    text = _extract_text(msg.content)
                    if text:
                        if not in_ai_block:
                            print()
                            in_ai_block = True
                        print(text, end="", flush=True)
                elif isinstance(msg, ToolMessage):
                    if in_ai_block:
                        print("\n")
                        in_ai_block = False
                    # Feature 7: colour tool calls cyan
                    print(_c(_C.CYAN, f"  [tool: {msg.name}]"))
        except KeyboardInterrupt:
            if in_ai_block:
                print()
            print(_c(_C.YELLOW, "[interrupted]"))
            return
        except Exception as exc:
            if in_ai_block:
                print("\n")
                in_ai_block = False
            print(_c(_C.RED, f"[error] {exc}"))
            return

        if in_ai_block:
            print("\n")

        # Feature 5: display token/cost after each Claude API response
        if last_usage and state.model in _COST_PER_1M:
            in_tok = last_usage.get("input_tokens", 0)
            out_tok = last_usage.get("output_tokens", 0)
            in_price, out_price = _COST_PER_1M[state.model]
            cost = (in_tok * in_price + out_tok * out_price) / 1_000_000
            print(_c(_C.DIM, f"  [tokens in={in_tok} out={out_tok} cost≈${cost:.4f}]"))
            last_usage = {}

        # Check for a pending interrupt (write-protection gate)
        try:
            tasks = graph.get_state(cfg).tasks or []
            intr = next((i for t in tasks for i in getattr(t, "interrupts", [])), None)
            if intr is not None:
                # Feature 7: colour confirm prompt yellow+bold
                print(_c(_C.YELLOW + _C.BOLD, format_interrupt(intr.value)))
                answer = input("Proceed? [y/N] ").strip()
                inp = Command(resume=answer if _is_affirmative(answer) else "no")
                continue
        except Exception:
            pass
        break


# ── Feature 10: Startup health brief ─────────────────────────────────────────


def _startup_brief() -> None:
    """Probe key services at startup and print a one-line health summary."""
    checks = {
        "control_plane": config.CONTROL_PLANE_URL + "/health",
        "ray": config.RAY_SERVE_URL + "/health",
        "mlflow": config.MLFLOW_URL + "/health",
    }
    statuses = []
    for name, url in checks.items():
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(url)
                label = "UP" if resp.is_success else f"ERR{resp.status_code}"
        except Exception:
            label = "DOWN"
        statuses.append(f"{name}:{label}")
    all_up = all("UP" in s for s in statuses)
    color = _C.DIM if all_up else _C.YELLOW
    print(_c(color, "Services: " + " | ".join(statuses)) + "\n")


def main() -> None:
    info = check_backend()
    if not info["ok"]:
        # Name the backend that actually failed. This used to blame Ollama whatever had gone
        # wrong, which sent an operator hunting everywhere except the credential that was
        # rejected — the exact failure mode of 2026-08-20.
        tried = ", ".join(info.get("skipped") or [info["type"]])
        print(f"Error: no usable LLM backend. Tried, in preference order: {tried}.")
        print(
            f"       The preferred one is '{info['type']}' — fix {info.get('fix', 'its config')}."
        )
        sys.exit(1)
    if info.get("skipped"):
        # Falling back silently would change every answer's quality without telling anyone.
        print(
            f"Warning: {', '.join(info['skipped'])} unusable — falling back to "
            f"'{info['type']}'. Fix {_FIX_HINT.get(info['skipped'][0], 'its config')}."
        )

    state = CliState(model=info["model"])
    graph = build_graph(model=state.model)

    if info["type"] == "claude":
        backend_label = f"claude · {state.model}"
    elif info["type"] == "azure":
        backend_label = f"azure · {state.model}  ·  {config.AZURE_OPENAI_ENDPOINT}"
    else:
        backend_label = f"ollama · {state.model}  ·  {config.AGENT_OLLAMA_URL}"

    print(f"Skipper · ExaMLOps agent  ({backend_label})")
    print("Type a question, or /help for commands.\n")
    _startup_brief()  # Feature 10

    while True:
        try:
            text = input("skipper > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break
        if not text:
            continue
        try:
            is_cmd, graph = handle_slash(text, state, graph)
            if is_cmd:
                continue
        except SystemExit:
            print("Goodbye.")
            break
        run_turn(graph, state, text)
