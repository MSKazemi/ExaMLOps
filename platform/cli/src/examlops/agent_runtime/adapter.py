"""The framework seam (ADR 0144 decision 2): one ``AgentAdapter`` per agent framework.

::

    AgentAdapter                       # one per framework ("python", "langgraph", ...)
      framework: str
      capabilities() -> Capabilities   # honest: the runtime refuses what an adapter cannot do
      build(manifest, program) -> AgentRunnable

    AgentRunnable                      # one per hosted agent version
      invoke(thread, input, hooks)  -> RunResult    # start (or continue) a run
      resume(thread, value, hooks)  -> RunResult    # after an interrupt was resolved
      get_state(thread)             -> StateSnapshot
      state_schema()                -> dict | None  # for the ADR 0146 d5 compatibility gate

The runtime owns tenancy, threads, runs, the multitask strategies, leases, admission, routing and
suspension; an adapter owns only *how one framework steps a graph*. ``build`` receives the
already-loaded program object (the ``code.entrypoint`` of the manifest) - loading code is the
runtime's decision, gated by an allow-list, not the adapter's.

``RunHooks`` is what an adapter calls back into while it steps: write a checkpoint, check
whether the runtime asked it to stop at this boundary, and make a brokered tool call.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from examlops.agent_runtime.types import Capabilities, RunResult, StateSnapshot

__all__ = ["AgentAdapter", "AgentRunnable", "RunHooks", "adapter_for", "register_adapter"]


@runtime_checkable
class RunHooks(Protocol):
    thread: dict[str, Any]
    run: dict[str, Any]

    def checkpoint(self, state: dict[str, Any], next_node: str | None) -> int: ...

    def stop_requested(self) -> str | None: ...

    def step(self) -> None:
        """Count one step taken by an adapter whose framework checkpoints for itself (so the
        run's step budget and lease still apply). ``LeaseLost`` if the lease is gone."""
        ...

    def call_tool(
        self, node: str, checkpoint_id: int | str, seq: int, tool: str, args: dict[str, Any]
    ) -> dict[str, Any]: ...

    def interrupt_value(
        self, node: str, checkpoint_id: int | str, seq: int, payload: dict[str, Any]
    ) -> Any: ...

    def model(self, role: str) -> dict[str, Any]: ...

    def sandbox_exec(self, command: str, *, timeout: float | None = None) -> dict[str, Any]: ...


@runtime_checkable
class AgentRunnable(Protocol):
    def invoke(self, thread: dict[str, Any], input: Any, hooks: RunHooks) -> RunResult: ...

    def resume(self, thread: dict[str, Any], hooks: RunHooks) -> RunResult: ...

    def get_state(self, thread: dict[str, Any]) -> StateSnapshot: ...

    def state_schema(self) -> dict[str, Any] | None: ...


@runtime_checkable
class AgentAdapter(Protocol):
    framework: str

    def capabilities(self) -> Capabilities: ...

    def build(self, manifest: dict[str, Any], program: Any, store: Any) -> AgentRunnable: ...


_ADAPTERS: dict[str, AgentAdapter] = {}


def _builtins() -> None:
    from examlops.agent_runtime.engine import PythonAdapter
    from examlops.agent_runtime.langgraph_adapter import LangGraphAdapter

    for a in (PythonAdapter(), LangGraphAdapter()):
        _ADAPTERS.setdefault(a.framework, a)


def register_adapter(adapter: AgentAdapter) -> None:
    """Register (or replace) the adapter for ``adapter.framework``."""
    _builtins()
    _ADAPTERS[adapter.framework] = adapter


def adapter_for(framework: str) -> AgentAdapter:
    """The adapter for ``framework``; ``LookupError`` names the ones that exist."""
    _builtins()
    found = _ADAPTERS.get(framework)
    if found is None:
        raise LookupError(
            f"no agent adapter for framework {framework!r} (have: {', '.join(sorted(_ADAPTERS))})"
        )
    return found
