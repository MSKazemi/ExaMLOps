"""Value types of the agent runtime contract (ADR 0144 decision 2)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "ACTIVE_RUN_STATES",
    "RUN_STATES",
    "THREAD_STATES",
    "Capabilities",
    "Interrupt",
    "LeaseLost",
    "RunResult",
    "RuntimeRefusal",
    "StateSnapshot",
]

#: ``active -> idle -> suspended -> resumed(active) | closed`` (ADR 0144 d4); ``quarantined`` is
#: the ADR 0146 d4 in-flight rollback policy.
THREAD_STATES = ("active", "idle", "suspended", "closed", "quarantined")
RUN_STATES = (
    "pending",  # queued (a new input, or behind a busy thread under `enqueue`)
    "running",  # a worker holds the thread lease and is stepping it
    "interrupted",  # parked on a human decision (HITL) - survives restarts
    "success",
    "error",
    "cancelled",
    "superseded",  # stopped at a step boundary by a newer input (`interrupt` strategy)
    "rolled_back",  # stopped and its checkpoints discarded (`rollback` strategy)
    "rejected",  # refused before it ran (quarantined thread, unresolved model, ...)
    "budget_exceeded",
)
#: A thread with a run in one of these is busy: a second input meets its multitask strategy.
ACTIVE_RUN_STATES = ("pending", "running", "interrupted")


class RuntimeRefusal(RuntimeError):
    """The runtime refused an operation. ``code`` is stable; ``status`` is the HTTP mapping."""

    def __init__(self, code: str, reason: str, *, status: int = 409) -> None:
        self.code = code
        self.reason = reason
        self.status = status
        super().__init__(f"{code}: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.reason}


@dataclass(frozen=True)
class Capabilities:
    """What an adapter can genuinely do (ADR 0109 honesty rule). Must not overstate."""

    durable: bool = False  # resumes mid-run from a checkpoint after a crash
    interrupts: bool = False  # can park on a human decision and resume later
    streaming: bool = False
    idempotent_nodes: bool = False  # derives ADR 0144 d3 idempotency keys for tool calls
    cancel: bool = False  # stops cooperatively at a step boundary
    rollback: bool = False  # can discard a run's checkpoints (multitask `rollback`)
    state_schema: bool = False  # exports a state schema for the ADR 0146 d5 gate

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


class Interrupt(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised inside a node to park the run on a human decision."""

    def __init__(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        self.kind = kind  # "approval" | "input"
        self.key = key
        self.payload = payload
        super().__init__(f"interrupt({kind})")


class LeaseLost(BaseException):  # noqa: N818 - a control-flow signal, not an error
    """This worker no longer holds the thread's lease: another worker has taken the run over.

    A ``BaseException`` on purpose: agent code wrapping ``ctx.call_tool`` in ``except
    Exception`` must not swallow it and carry on stepping a run someone else now owns.
    """

    def __init__(self, thread_id: str, holder: str) -> None:
        self.thread_id = thread_id
        self.holder = holder
        super().__init__(f"lease on {thread_id} lost by {holder}")


@dataclass
class RunResult:
    status: str
    output: Any = None
    interrupt: dict[str, Any] | None = None
    checkpoint_id: int | str | None = None
    steps: int = 0
    error: str | None = None


@dataclass
class StateSnapshot:
    thread_id: str
    values: dict[str, Any]
    next: list[str] = field(default_factory=list)
    checkpoint_id: int | str | None = None
    agent_version_id: str | None = None
    state_schema_version: int | None = None
