"""The Agent Runtime (ADR 0144) and its sandbox seam (ADR 0145 d6).

Agents are a deployable workload kind: an ``AgentVersion`` (ADR 0146) is hosted by a runtime that
implements threads, runs, interrupts and durable state over framework adapters. See
:mod:`examlops.agent_runtime.runtime` for the contract and ``docs/guides/agent-runtime.md``.
"""

from examlops.agent_runtime.adapter import (
    AgentAdapter,
    AgentRunnable,
    RunHooks,
    adapter_for,
    register_adapter,
)
from examlops.agent_runtime.engine import END, AgentProgram, Node, NodeContext, PythonAdapter
from examlops.agent_runtime.gateway import BrokerGateway, ToolGateway
from examlops.agent_runtime.routing import canary_bucket, owner, starts_on_canary
from examlops.agent_runtime.runtime import AgentRuntime, derive_idempotency_key, load_entrypoint
from examlops.agent_runtime.sandbox import (
    ISOLATION_RANK,
    AgentSandboxK8sProvider,
    ApptainerProvider,
    DockerProvider,
    NoSandbox,
    SandboxCapabilities,
    SandboxHandle,
    SandboxProvider,
    SandboxRefused,
    select_provider,
)
from examlops.agent_runtime.snapshot import (
    compile_agent_snapshot,
    load_snapshot_file,
    validate_snapshot,
    write_snapshot,
)
from examlops.agent_runtime.store import AgentStateStore, default_path
from examlops.agent_runtime.types import (
    Capabilities,
    Interrupt,
    RunResult,
    RuntimeRefusal,
    StateSnapshot,
)

__all__ = [
    "END",
    "ISOLATION_RANK",
    "AgentAdapter",
    "AgentProgram",
    "AgentRunnable",
    "AgentRuntime",
    "AgentSandboxK8sProvider",
    "AgentStateStore",
    "ApptainerProvider",
    "BrokerGateway",
    "Capabilities",
    "DockerProvider",
    "Interrupt",
    "NoSandbox",
    "Node",
    "NodeContext",
    "PythonAdapter",
    "RunHooks",
    "RunResult",
    "RuntimeRefusal",
    "SandboxCapabilities",
    "SandboxHandle",
    "SandboxProvider",
    "SandboxRefused",
    "StateSnapshot",
    "ToolGateway",
    "adapter_for",
    "canary_bucket",
    "compile_agent_snapshot",
    "default_path",
    "derive_idempotency_key",
    "load_entrypoint",
    "load_snapshot_file",
    "owner",
    "register_adapter",
    "select_provider",
    "starts_on_canary",
    "validate_snapshot",
    "write_snapshot",
]
