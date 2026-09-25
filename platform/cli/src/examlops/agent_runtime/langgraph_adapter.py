"""The LangGraph adapter (ADR 0144 decision 2) - the reference implementation.

Delegates what LangGraph already does well: the graph engine and its checkpointer. The agent's
entrypoint returns an **uncompiled** ``StateGraph``; the adapter compiles it with a
``SqliteSaver`` on the agent state store file, so LangGraph's own checkpoints live in the
serving-plane store beside the runtime's tables (``checkpoints``/``writes`` vs ``rt_*``) and
never in ``platform.db``. Each checkpoint's metadata carries ``agent_version_id`` and ``run_id``.

The runtime steps the graph with ``stream(stream_mode="updates")`` so it can stop between
super-steps (multitask ``interrupt``) and detects ``__interrupt__`` to park a run on a human.

Capabilities are stated, not assumed: this adapter does **not** discard a run's checkpoints
(``rollback=False`` - LangGraph keeps history; forking from an older checkpoint is a different
operation) and does not derive ADR 0144 d3 tool keys (``idempotent_nodes=False``), so the runtime
refuses to host a LangGraph version whose manifest asks for the ``rollback`` strategy.

``langgraph`` (and ``langgraph-checkpoint-sqlite``) is imported lazily: the adapter registers
without it and fails with a clear message only when a LangGraph agent is actually built.
"""

from __future__ import annotations

import typing
from typing import Any

from examlops.agent_runtime.adapter import RunHooks
from examlops.agent_runtime.types import Capabilities, RunResult, StateSnapshot

__all__ = ["LangGraphAdapter", "LangGraphRunnable"]


class LangGraphRunnable:
    def __init__(self, manifest: dict[str, Any], builder: Any, store: Any) -> None:
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "the LangGraph adapter needs langgraph and langgraph-checkpoint-sqlite "
                "(pip install 'langgraph-checkpoint-sqlite')"
            ) from exc
        from examlops.resilience.db import connect

        self.manifest = manifest
        self.builder = builder
        self._conn = connect(store.path, row_factory=None)
        saver = SqliteSaver(self._conn)
        saver.setup()
        self.graph = builder.compile(checkpointer=saver)

    def _config(self, thread: dict[str, Any], hooks: RunHooks | None = None) -> dict[str, Any]:
        cfg: dict[str, Any] = {"configurable": {"thread_id": thread["thread_id"]}}
        if hooks is not None:
            cfg["metadata"] = {
                "agent_version_id": thread["version_id"],
                "run_id": hooks.run["run_id"],
            }
        return cfg

    def _drive(self, source: Any, thread: dict[str, Any], hooks: RunHooks) -> RunResult:
        cfg = self._config(thread, hooks)
        steps = 0
        for chunk in self.graph.stream(source, cfg, stream_mode="updates"):
            if isinstance(chunk, dict) and "__interrupt__" in chunk:
                intr = chunk["__interrupt__"]
                first = intr[0] if isinstance(intr, (list, tuple)) and intr else intr
                value = getattr(first, "value", first)
                return RunResult(
                    "interrupted",
                    interrupt={"kind": "input", "payload": value},
                    steps=steps,
                )
            steps += 1
            # LangGraph checkpoints itself, so the runtime's checkpoint hook never runs here:
            # count the super-step explicitly, or `budgets.max_steps` would never fire and the
            # lease would never be checked for a LangGraph agent.
            hooks.step()
            stop = hooks.stop_requested()
            if stop:
                return RunResult(f"stop:{stop}", steps=steps)
        snap = self.graph.get_state(self._config(thread))
        values = dict(snap.values or {})
        return RunResult(
            "success",
            output=values.get("output"),
            checkpoint_id=(snap.config or {}).get("configurable", {}).get("checkpoint_id"),
            steps=steps,
        )

    def invoke(self, thread: dict[str, Any], input: Any, hooks: RunHooks) -> RunResult:
        # A run another worker started and crashed in continues from LangGraph's own checkpoint
        # (``stream(None)``) instead of re-entering its input.
        source = None if getattr(hooks, "continuing", False) else input
        return self._drive(source, thread, hooks)

    def resume(self, thread: dict[str, Any], hooks: RunHooks) -> RunResult:
        from langgraph.types import Command

        return self._drive(Command(resume=getattr(hooks, "resume_value", None)), thread, hooks)

    def get_state(self, thread: dict[str, Any]) -> StateSnapshot:
        snap = self.graph.get_state(self._config(thread))
        meta = snap.metadata or {}
        return StateSnapshot(
            thread["thread_id"],
            dict(snap.values or {}),
            list(snap.next or ()),
            (snap.config or {}).get("configurable", {}).get("checkpoint_id"),
            meta.get("agent_version_id"),
            (self.manifest.get("state") or {}).get("schema_version"),
        )

    def state_schema(self) -> dict[str, Any] | None:
        """Fields from the graph's state type and its node names. No field carries a default
        (LangGraph does not declare one), so an added field reads as incompatible - honest."""
        schema_type = getattr(self.builder, "state_schema", None) or getattr(
            self.builder, "schema", None
        )
        try:
            hints = typing.get_type_hints(schema_type) if schema_type is not None else {}
        except Exception:  # noqa: BLE001 - an un-introspectable schema is "unknown", i.e. inert
            return None
        nodes = [n for n in getattr(self.builder, "nodes", {}) if not n.startswith("__")]
        if not hints or not nodes:
            return None
        return {
            "fields": {k: {"type": getattr(v, "__name__", str(v))} for k, v in hints.items()},
            "nodes": nodes,
        }


class LangGraphAdapter:
    framework = "langgraph"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            durable=True,
            interrupts=True,
            streaming=True,
            idempotent_nodes=False,
            cancel=True,
            rollback=False,
            state_schema=True,
        )

    def build(self, manifest: dict[str, Any], program: Any, store: Any) -> LangGraphRunnable:
        if not hasattr(program, "compile"):
            raise TypeError("framework 'langgraph' needs an uncompiled StateGraph entrypoint")
        return LangGraphRunnable(manifest, program, store)
