"""The framework-neutral durable adapter (ADR 0144 decision 2): a journaling step engine.

For agents written as plain Python (or wrapping a framework with no durable execution of its
own), the program is a list of named nodes. The engine runs them one at a time and writes a
checkpoint to the agent state store after **every** node - the super-step boundary - so a
crashed worker's run resumes on another worker from the last completed node.

A re-executed node must not repeat its side effects. Every tool call a node makes goes through
the runtime with an idempotency key derived as ``hash(thread_id, checkpoint_id, node, call_seq)``
(ADR 0144 d3): the checkpoint the node started from, the node's name and the call's position in
the node. A re-execution presents the same key, and the runtime's tool journal returns the
stored result instead of calling the tool a second time.

The ADR names DBOS as the candidate library for this role, to be chosen by measurement. This
engine is the built-in journal that the measurement would be compared against: it needs no
dependency and journals into the same store as everything else. DBOS is not adopted.

A node is ``fn(state, ctx) -> dict | None`` returning the fields it changes. ``"__next__"`` in
the result jumps to a named node (or ``"__end__"``); otherwise the next node in the list runs.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from examlops.agent_runtime.adapter import RunHooks
from examlops.agent_runtime.types import Capabilities, RunResult, StateSnapshot

__all__ = ["END", "AgentProgram", "Node", "NodeContext", "PythonAdapter", "PythonRunnable"]

END = "__end__"


@dataclass(frozen=True)
class Node:
    name: str
    fn: Callable[[dict[str, Any], NodeContext], dict[str, Any] | None]
    #: Declared by the author (ADR 0144 d3): a node that is not idempotent must route its side
    #: effects through ``ctx.call_tool`` - which is what makes its re-execution safe.
    idempotent: bool = True


@dataclass
class AgentProgram:
    nodes: list[Node]
    state_schema: dict[str, Any] | None = None
    initial_state: dict[str, Any] = field(default_factory=dict)
    #: How a new input enters the state; default ``state["input"] = input``.
    on_input: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        names = [n.name for n in self.nodes]
        if not names:
            raise ValueError("an agent program needs at least one node")
        if len(set(names)) != len(names) or END in names:
            raise ValueError("node names must be unique and may not be '__end__'")


class NodeContext:
    """What a node may do besides computing: brokered tools, interrupts, models, a sandbox."""

    def __init__(self, hooks: RunHooks, node: str, checkpoint_id: int | str) -> None:
        self._hooks = hooks
        self.node = node
        self.checkpoint_id = checkpoint_id
        self._tool_seq = 0
        self._intr_seq = 0

    @property
    def thread_id(self) -> str:
        return str(self._hooks.thread["thread_id"])

    @property
    def run_id(self) -> str:
        return str(self._hooks.run["run_id"])

    def call_tool(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call ``tool`` through the tool gateway, with the derived idempotency key."""
        seq = self._tool_seq
        self._tool_seq += 1
        return self._hooks.call_tool(self.node, self.checkpoint_id, seq, tool, dict(args or {}))

    def interrupt(self, payload: dict[str, Any]) -> Any:
        """Park the run until a human answers; returns the answer once the run is resumed."""
        seq = self._intr_seq
        self._intr_seq += 1
        return self._hooks.interrupt_value(self.node, self.checkpoint_id, seq, payload)

    def model(self, role: str) -> dict[str, Any]:
        """The model bound to ``role`` for THIS run: resolved once at run start (ADR 0146 d2)."""
        return self._hooks.model(role)

    def sandbox_exec(self, command: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Run ``command`` in this session's sandbox (ADR 0145 d6)."""
        return self._hooks.sandbox_exec(command, timeout=timeout)


class PythonRunnable:
    def __init__(self, manifest: dict[str, Any], program: AgentProgram, store: Any) -> None:
        self.manifest = manifest
        self.program = program
        self.store = store
        self._by_name = {n.name: n for n in program.nodes}
        self._order = [n.name for n in program.nodes]

    def _after(self, name: str) -> str:
        i = self._order.index(name)
        return self._order[i + 1] if i + 1 < len(self._order) else END

    def invoke(self, thread: dict[str, Any], input: Any, hooks: RunHooks) -> RunResult:
        cp = self.store.latest_checkpoint(thread["thread_id"])
        if cp is not None and cp["run_id"] == hooks.run["run_id"]:
            return self._loop(cp, hooks)  # a crashed run continuing on another worker
        base = copy.deepcopy(cp["state"]) if cp else copy.deepcopy(self.program.initial_state)
        if self.program.on_input is not None:
            state = self.program.on_input(base, input)
        else:
            base["input"] = input
            state = base
        cid = hooks.checkpoint(state, self._order[0])
        return self._loop(
            {"checkpoint_id": cid, "state": state, "next_node": self._order[0]}, hooks
        )

    def resume(self, thread: dict[str, Any], hooks: RunHooks) -> RunResult:
        cp = self.store.latest_checkpoint(thread["thread_id"])
        if cp is None or cp["run_id"] != hooks.run["run_id"]:
            return RunResult("error", error="no checkpoint of this run to resume from")
        return self._loop(cp, hooks)

    def _loop(self, cp: dict[str, Any], hooks: RunHooks) -> RunResult:
        state: dict[str, Any] = copy.deepcopy(cp["state"])
        node_name: str | None = cp["next_node"]
        cid = cp["checkpoint_id"]
        steps = 0
        while node_name and node_name != END:
            stop = hooks.stop_requested()
            if stop:
                return RunResult(f"stop:{stop}", checkpoint_id=cid, steps=steps)
            node = self._by_name.get(node_name)
            if node is None:
                return RunResult("error", checkpoint_id=cid, error=f"unknown node {node_name!r}")
            ctx = NodeContext(hooks, node_name, cid)
            updates = node.fn(copy.deepcopy(state), ctx) or {}
            if not isinstance(updates, dict):
                return RunResult(
                    "error",
                    checkpoint_id=cid,
                    error=f"node {node_name} returned {type(updates).__name__}, not a dict",
                )
            nxt = updates.pop("__next__", None)
            state.update(updates)
            node_name = nxt or self._after(node_name)
            if node_name != END and node_name not in self._by_name:
                return RunResult("error", checkpoint_id=cid, error=f"unknown node {node_name!r}")
            cid = hooks.checkpoint(state, node_name)
            steps += 1
        return RunResult("success", output=state.get("output"), checkpoint_id=cid, steps=steps)

    def get_state(self, thread: dict[str, Any]) -> StateSnapshot:
        cp = self.store.latest_checkpoint(thread["thread_id"])
        if cp is None:
            return StateSnapshot(thread["thread_id"], copy.deepcopy(self.program.initial_state))
        nxt = [] if cp["next_node"] in (None, END) else [cp["next_node"]]
        return StateSnapshot(
            thread["thread_id"],
            cp["state"],
            nxt,
            cp["checkpoint_id"],
            cp["agent_version_id"],
            cp["state_schema_version"],
        )

    def state_schema(self) -> dict[str, Any] | None:
        return self.program.state_schema


class PythonAdapter:
    framework = "python"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            durable=True,
            interrupts=True,
            streaming=False,
            idempotent_nodes=True,
            cancel=True,
            rollback=True,
            state_schema=True,
        )

    def build(self, manifest: dict[str, Any], program: Any, store: Any) -> PythonRunnable:
        if not isinstance(program, AgentProgram):
            raise TypeError(
                f"framework 'python' needs an AgentProgram entrypoint, got {type(program).__name__}"
            )
        return PythonRunnable(manifest, program, store)
