from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from exa_agent import config
from exa_agent.confirm import _is_affirmative
from exa_agent.graph import build_graph
from exa_agent.llm import check_ollama

_COMMANDS = {
    "/help": "Show this help",
    "/tools": "List available tools",
    "/new": "Start a fresh conversation thread",
    "/resume <id>": "Resume a saved thread",
    "/threads": "List saved thread ids",
    "/model <name>": "Switch the Ollama model",
    "/report": "Generate a platform status report",
    "/exit": "Quit",
}


@dataclass
class CliState:
    thread_id: str = field(default_factory=lambda: f"cli-{uuid.uuid4().hex[:8]}")
    model: str = config.AGENT_MODEL


def format_interrupt(payload: dict) -> str:
    return f"[confirm] {payload.get('action')}: {payload.get('summary', '')}"


def handle_slash(text: str, state: CliState, graph=None) -> bool:
    """Handle a slash-command. Returns True if the input was a command."""
    if not text.startswith("/"):
        return False
    parts = text.split(maxsplit=1)
    cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")

    if cmd == "/help":
        for name, desc in _COMMANDS.items():
            print(f"  {name:<16} {desc}")
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
            print(f"Model set to {state.model} (restart turn to apply)")
        else:
            print("Usage: /model <name>")
    elif cmd in ("/exit", "/quit"):
        raise SystemExit(0)
    else:
        print(f"Unknown command: {cmd}")
    return True


def _print_new_messages(messages: list, already: int) -> int:
    for msg in messages[already:]:
        if isinstance(msg, ToolMessage):
            print(f"  [tool: {msg.name}]")
        elif isinstance(msg, AIMessage) and msg.content:
            print(f"\n{msg.content}\n")
    return len(messages)


def run_turn(graph, state: CliState, user_input: str) -> None:
    cfg = {"configurable": {"thread_id": state.thread_id}}
    # Baseline: messages already in this thread's history so we only print
    # messages produced by THIS turn (graph.invoke returns full accumulated history).
    try:
        printed = len(graph.get_state(cfg).values.get("messages", []))
    except Exception:
        printed = 0
    inp: object = {"messages": [HumanMessage(content=user_input)]}
    while True:
        result = graph.invoke(inp, cfg)
        if "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            print(format_interrupt(payload))
            answer = input("Proceed? [y/N] ").strip()
            inp = Command(resume=answer if _is_affirmative(answer) else "no")
            continue
        printed = _print_new_messages(result.get("messages", []), printed)
        break


def main() -> None:
    if not check_ollama():
        print(f"Error: Ollama is not reachable at {config.AGENT_OLLAMA_URL}. Start it (ollama-tunnel start).")
        sys.exit(1)
    state = CliState()
    graph = build_graph(model=state.model)
    print(f"ExaMLOps Agent  (model: {state.model}  ·  ollama: {config.AGENT_OLLAMA_URL})")
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
            if handle_slash(text, state, graph):
                continue
        except SystemExit:
            print("Goodbye.")
            break
        run_turn(graph, state, text)
