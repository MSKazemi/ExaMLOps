from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from exa_agent import config
from exa_agent.confirm import _is_affirmative
from exa_agent.graph import build_graph
from exa_agent.llm import check_backend

_COMMANDS = {
    "/help": "Show this help",
    "/tools": "List available tools",
    "/new": "Start a fresh conversation thread",
    "/resume <id>": "Resume a saved thread",
    "/threads": "List saved thread ids",
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
        default_factory=lambda: config.ANTHROPIC_MODEL if config.ANTHROPIC_API_KEY else config.AGENT_MODEL
    )


def format_interrupt(payload: dict) -> str:
    return f"[confirm] {payload.get('action')}: {payload.get('summary', '')}"


def _extract_text(content) -> str:
    """Extract visible text from message content, filtering out thinking blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) and block.get("type") == "text"
            else (block if isinstance(block, str) else "")
            for block in content
        )
    return ""


def handle_slash(text: str, state: CliState, graph=None) -> tuple[bool, object]:
    """Handle a slash-command. Returns (was_command, graph).

    The graph may be replaced (e.g. on /model switch); callers must update
    their local reference from the returned value.
    """
    if not text.startswith("/"):
        return False, graph
    parts = text.split(maxsplit=1)
    cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")

    if cmd == "/help":
        for name, desc in _COMMANDS.items():
            print(f"  {name:<22} {desc}")
    elif cmd == "/tools":
        from exa_agent.tools import TOOLS

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
            seen = {
                c.config["configurable"]["thread_id"]
                for c in graph.checkpointer.list(None)
            }
            print("\n".join(sorted(seen)) or "(no saved threads)")
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

    while True:
        in_ai_block = False

        try:
            for item in graph.stream(inp, cfg, stream_mode="messages"):
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                msg, _meta = item
                if isinstance(msg, AIMessageChunk):
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
                    print(f"  [tool: {msg.name}]")
        except KeyboardInterrupt:
            if in_ai_block:
                print()
            print("[interrupted]")
            return
        except Exception as exc:
            if in_ai_block:
                print("\n")
                in_ai_block = False
            print(f"[error] {exc}")
            return

        if in_ai_block:
            print("\n")

        # Check for a pending interrupt (write-protection confirmation gate)
        try:
            tasks = graph.get_state(cfg).tasks or []
            intr = next(
                (i for t in tasks for i in getattr(t, "interrupts", [])),
                None,
            )
            if intr is not None:
                print(format_interrupt(intr.value))
                answer = input("Proceed? [y/N] ").strip()
                inp = Command(resume=answer if _is_affirmative(answer) else "no")
                continue
        except Exception:
            pass
        break


def main() -> None:
    info = check_backend()
    if not info["ok"]:
        print(
            f"Error: Ollama not reachable at {config.AGENT_OLLAMA_URL}. "
            "Start it (ollama-tunnel start) or set ANTHROPIC_API_KEY."
        )
        sys.exit(1)

    state = CliState(model=info["model"])
    graph = build_graph(model=state.model)

    if info["type"] == "claude":
        backend_label = f"claude · {state.model}"
    else:
        backend_label = f"ollama · {state.model}  ·  {config.AGENT_OLLAMA_URL}"

    print(f"ExaMLOps Agent  ({backend_label})")
    print("Type a question, or /help for commands.\n")

    while True:
        try:
            text = input("ExaMLOps Agent > ").strip()
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
