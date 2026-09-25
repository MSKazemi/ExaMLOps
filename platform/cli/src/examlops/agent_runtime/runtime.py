"""The Agent Runtime (ADR 0144): many agent versions, many tenants, one durable contract.

``threads · runs · interrupts · state`` over pluggable adapters, with everything the ADR puts in
the runtime rather than in each agent:

* **Sessions** (decision 4): addressable threads with the lifecycle
  ``active -> idle -> suspended -> resumed | closed``; an idle session releases its sandbox and
  its admission slot and resumes from the state store on the next input.
* **Versions and rollout** (ADR 0146 d4): a new session starts on ``Production`` - or on
  ``Canary`` for the configured share of *new* sessions - and stays pinned to that version.
  Rolled-back versions follow the recorded in-flight policy (``continue | interrupt |
  quarantine``); Production moves follow the recorded state-compatibility verdict.
* **Runs and multitask strategies** (decision 2): a second input on a busy thread meets the
  agent's ``reject | enqueue | interrupt | rollback`` strategy.
* **Durability** (decision 3): step checkpoints in the agent state store, a lease per thread,
  crash recovery on another worker, and derived idempotency keys so a re-executed node does not
  repeat a tool call.
* **Human approval** (ADR 0145 d5): a tool call that needs approval parks the run; the
  approval is stored, survives restarts, and the write then executes exactly once.
* **Honest capability** (ADR 0109): a version whose adapter cannot do what the runtime would
  have to promise is refused at deploy time, with the reason.
* **Static stability** (decision 5): configuration comes from the agent snapshot (kept as
  last-known-good in the state store); nothing on the request path reads ``platform.db``.
* **Replay shadow** (ADR 0146 d4): a candidate version is run against a recorded session with
  tools stubbed from the recording - no live side effect is possible.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import logging
import os
import threading
from collections.abc import Callable, Sequence
from typing import Any

from examlops.agent_runtime.adapter import AgentRunnable, adapter_for
from examlops.agent_runtime.gateway import BrokerGateway, ToolGateway
from examlops.agent_runtime.routing import owner, starts_on_canary
from examlops.agent_runtime.sandbox import (
    SandboxHandle,
    SandboxProvider,
    SandboxRefused,
    select_provider,
    stricter,
)
from examlops.agent_runtime.snapshot import validate_snapshot
from examlops.agent_runtime.store import HOLDING_STATUSES, AgentStateStore
from examlops.agent_runtime.types import (
    ACTIVE_RUN_STATES,
    Capabilities,
    Interrupt,
    LeaseLost,
    RunResult,
    RuntimeRefusal,
    StateSnapshot,
)
from examlops.tool_broker.grants import GrantSet, ToolCaller, parse_grant, resolve_subjects

__all__ = ["AgentRuntime", "derive_idempotency_key", "load_entrypoint"]

logger = logging.getLogger(__name__)

_DEFAULT_STRATEGY = "reject"  # today's Skipper lease behaviour
_MAX_INPUT_BYTES = 256 * 1024
#: Runs that may wait behind a busy thread under ``enqueue``: past this a new input is refused
#: (429) rather than letting one caller grow an unbounded backlog of runs and stored inputs.
_MAX_QUEUED_PER_THREAD = 32
_RESUMABLE = ("active", "idle", "suspended")


def derive_idempotency_key(thread_id: str, checkpoint_id: Any, node: str, call_seq: Any) -> str:
    """``hash(thread_id, checkpoint_id, node, call_seq)`` (ADR 0144 decision 3)."""
    blob = "\x00".join(str(p) for p in (thread_id, checkpoint_id, node, call_seq))
    return "ak-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _digest(args: dict[str, Any]) -> str:
    blob = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _servable_key(servable: str) -> str:
    s = servable.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    return s.strip("/").lower()


def load_entrypoint(manifest: dict[str, Any]) -> Any:
    """Import ``code.entrypoint`` (``module:attr``) - only from allow-listed module prefixes.

    ``EXAMLOPS_AGENT_ENTRYPOINT_ALLOW`` is a comma-separated list of module prefixes; unset, no
    code is loaded (fail closed). In production the code is the image named by digest in the
    manifest; importing into the runtime's own process is the dev/compose path. A callable
    attribute that is not itself a program (no ``nodes`` and no ``compile``) is called once as
    a factory.
    """
    entry = str((manifest.get("code") or {}).get("entrypoint", ""))
    module, _, attr = entry.partition(":")
    allow = [p.strip() for p in os.getenv("EXAMLOPS_AGENT_ENTRYPOINT_ALLOW", "").split(",")]
    allow = [p for p in allow if p]
    if not module or not attr:
        raise RuntimeRefusal(
            "bad_entrypoint", f"entrypoint {entry!r} is not 'module:attr'", status=422
        )
    if not any(module == p or module.startswith(p + ".") for p in allow):
        raise RuntimeRefusal(
            "entrypoint_not_allowed",
            f"module {module!r} is not in EXAMLOPS_AGENT_ENTRYPOINT_ALLOW",
            status=403,
        )
    obj: Any = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if callable(obj) and not hasattr(obj, "nodes") and not hasattr(obj, "compile"):
        obj = obj()
    return obj


class _Hooks:
    """The adapter's view of one run (implements ``RunHooks``)."""

    def __init__(
        self,
        rt: AgentRuntime,
        thread: dict[str, Any],
        run: dict[str, Any],
        manifest: dict[str, Any],
        holder: str | None = None,
    ) -> None:
        self.rt = rt
        self.thread = thread
        self.run = run
        self.manifest = manifest
        self.holder = holder or rt.holder()
        self.steps = int(run.get("steps") or 0)
        self.max_steps = (manifest.get("budgets") or {}).get("max_steps")
        self.resume_value: Any = None
        self.continuing = False

    # checkpoints and stopping ------------------------------------------------------------------

    def hold_lease(self) -> None:
        """Renew the thread's lease, or stop: ``LeaseLost`` when another worker now holds it.

        Checked before every tool call and every checkpoint, so a worker that stalled past its
        lease (and whose run was recovered elsewhere) cannot perform a second side effect or
        write a checkpoint over the new owner's progress.
        """
        tid = self.thread["thread_id"]
        if not self.rt.store.acquire_lease(tid, self.holder, self.rt.lease_ttl):
            raise LeaseLost(tid, self.holder)

    def checkpoint(self, state: dict[str, Any], next_node: str | None) -> int:
        rt = self.rt
        self.hold_lease()
        cid = rt.store.put_checkpoint(
            self.thread["thread_id"],
            run_id=self.run["run_id"],
            state=state,
            next_node=next_node,
            agent_version_id=self.run["version_id"],
            state_schema_version=(self.manifest.get("state") or {}).get("schema_version"),
        )
        self.steps += 1
        rt.store.update_run(self.run["run_id"], steps=self.steps)
        return cid

    def step(self) -> None:
        self.hold_lease()
        self.steps += 1
        self.rt.store.update_run(self.run["run_id"], steps=self.steps)

    def stop_requested(self) -> str | None:
        row = self.rt.store.get_run(self.run["run_id"])
        if row and row.get("stop_requested"):
            return str(row["stop_requested"])
        if self.max_steps is not None and self.steps >= int(self.max_steps):
            return "budget"
        return None

    # tools, interrupts, models, sandbox ---------------------------------------------------------

    def call_tool(
        self, node: str, checkpoint_id: int | str, seq: int, tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        rt = self.rt
        tid = self.thread["thread_id"]
        key = derive_idempotency_key(tid, checkpoint_id, node, seq)
        done = rt.store.get_tool_result(key)
        if done is not None:  # a re-executed node: the journal answers, the tool does not run
            rt.store.log_event(
                "tool_replayed",
                tenant=self.thread["tenant"],
                thread_id=tid,
                run_id=self.run["run_id"],
                detail={"tool": tool, "key": key},
            )
            return {**done["result"], "replayed": True}
        intr = rt.store.get_interrupt(key)
        approved = False
        if intr is not None and intr["status"] != "open":
            value = intr.get("value") or {}
            if not (isinstance(value, dict) and value.get("approve") is True):
                result = {
                    "ok": False,
                    "code": "approval_rejected",
                    "error": f"a human rejected the call to {tool}",
                }
                rt.store.put_tool_result(
                    key,
                    thread_id=tid,
                    run_id=self.run["run_id"],
                    seq=seq,
                    tool=tool,
                    args_digest=_digest(args),
                    result=result,
                )
                return result
            approved = True
        self.hold_lease()  # never act for a run another worker has taken over
        caller = ToolCaller(
            agent=self.thread["agent"],
            version_id=self.run["version_id"],
            on_behalf_of=self.thread.get("principal"),
            session=tid,
            correlation_id=self.run["run_id"],
        )
        result = rt.gateway.call(
            caller, tool, args, approved=approved, idempotency_key=key, tenant=self.thread["tenant"]
        )
        if result.get("code") == "approval_required" and not approved:
            from examlops.tool_broker.broker import redact_args

            raise Interrupt(
                "approval",
                key,
                {"tool": tool, "args": redact_args(args), "reason": result.get("error")},
            )
        rt.store.put_tool_result(
            key,
            thread_id=tid,
            run_id=self.run["run_id"],
            seq=seq,
            tool=tool,
            args_digest=_digest(args),
            result=result,
        )
        return result

    def interrupt_value(
        self, node: str, checkpoint_id: int | str, seq: int, payload: dict[str, Any]
    ) -> Any:
        key = derive_idempotency_key(
            self.thread["thread_id"], checkpoint_id, node, f"interrupt-{seq}"
        )
        intr = self.rt.store.get_interrupt(key)
        if intr is not None and intr["status"] != "open":
            return intr.get("value")
        raise Interrupt("input", key, payload)

    def model(self, role: str) -> dict[str, Any]:
        resolved = self.run.get("resolved_models") or {}
        if role not in resolved:
            raise KeyError(f"no model bound to role {role!r}")
        return dict(resolved[role])

    def sandbox_exec(self, command: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self.rt._sandbox_exec(self.thread, self.manifest, command, timeout=timeout)


class AgentRuntime:
    def __init__(
        self,
        store: AgentStateStore,
        *,
        snapshot: dict[str, Any] | None = None,
        gateway: ToolGateway | None = None,
        program_loader: Callable[[dict[str, Any]], Any] | None = None,
        worker_id: str = "worker-1",
        peers: Sequence[str] = (),
        lease_ttl: float = 60.0,
        idle_after: float = 300.0,
        suspend_after: float = 900.0,
        require_durable: bool = True,
        sandbox_providers: Sequence[SandboxProvider] = (),
    ) -> None:
        self.store = store
        self.worker_id = worker_id
        self.peers = tuple(peers)
        self.lease_ttl = float(lease_ttl)
        self.idle_after = float(idle_after)
        self.suspend_after = float(suspend_after)
        self.require_durable = require_durable
        self.program_loader = program_loader or load_entrypoint
        self.gateway: ToolGateway = gateway or BrokerGateway(grant_resolver=self._grants_for)
        self.sandbox_providers = list(sandbox_providers)
        self._lock = threading.RLock()
        self._hosted: dict[str, tuple[Capabilities, AgentRunnable, dict[str, Any]]] = {}
        self._sandboxes: dict[str, tuple[SandboxProvider, SandboxHandle]] = {}
        self._snap: dict[str, Any] | None = None
        if snapshot is not None:
            self.apply_snapshot(snapshot)
        else:
            lkg = store.load_snapshot()
            if lkg is not None and not validate_snapshot(lkg):
                self._snap = lkg
                logger.info(
                    "agent runtime started from last-known-good snapshot %s", lkg.get("generation")
                )

    # -- configuration (the snapshot) -----------------------------------------------------------

    @property
    def snapshot(self) -> dict[str, Any] | None:
        return self._snap

    def apply_snapshot(self, doc: dict[str, Any]) -> bool:
        """Adopt a snapshot. Returns False (and keeps the current one) when it is older.

        Raises ``ValueError`` for an invalid or tampered document.
        """
        problems = validate_snapshot(doc)
        if problems:
            raise ValueError("; ".join(problems))
        with self._lock:
            cur = self._snap
            if cur is not None and int(doc["generation"]) < int(cur["generation"]):
                return False
            self._snap = doc
            self.store.save_snapshot(doc)
        self._apply_retirements(doc)
        return True

    def _need_snap(self) -> dict[str, Any]:
        if self._snap is None:
            raise RuntimeRefusal("no_snapshot", "the runtime has no agent snapshot yet", status=503)
        return self._snap

    def _grants_for(self, caller: ToolCaller) -> GrantSet | None:
        grants = (self._snap or {}).get("grants", {})
        for subject in resolve_subjects(caller):
            docs = grants.get(subject)
            if docs:
                return GrantSet(subject, {t: parse_grant(t, d) for t, d in docs.items()})
        return None

    def _manifest(self, version_id: str) -> dict[str, Any]:
        m = self._need_snap()["versions"].get(version_id)
        if m is None:
            raise RuntimeRefusal(
                "version_unknown", f"agent version {version_id} is not in the snapshot", status=404
            )
        return m

    # -- hosting and honest capability (ADR 0144 d2, verification 5) ----------------------------

    def deploy(self, version_id: str) -> dict[str, Any]:
        """Host a version, refusing it when its adapter cannot keep the runtime's promises."""
        caps, _, manifest = self._host(version_id)
        return {
            "ok": True,
            "version_id": version_id,
            "framework": (manifest.get("code") or {}).get("framework", "python"),
            "capabilities": caps.as_dict(),
        }

    def _host(self, version_id: str) -> tuple[Capabilities, AgentRunnable, dict[str, Any]]:
        with self._lock:
            if version_id in self._hosted:
                return self._hosted[version_id]
        manifest = self._manifest(version_id)
        framework = str((manifest.get("code") or {}).get("framework") or "python")
        try:
            adapter = adapter_for(framework)
        except LookupError as exc:
            raise RuntimeRefusal("unsupported_framework", str(exc), status=422) from exc
        caps = adapter.capabilities()
        strategy = (manifest.get("policy") or {}).get("multitask_strategy") or _DEFAULT_STRATEGY
        problems: list[str] = []
        if self.require_durable and not caps.durable:
            problems.append(
                f"durable runs are required but the {framework} adapter advertises durable: false"
            )
        if strategy == "rollback" and not caps.rollback:
            problems.append(
                f"multitask_strategy 'rollback' needs rollback, which the {framework} adapter "
                "does not support"
            )
        if strategy == "interrupt" and not caps.cancel:
            problems.append(
                f"multitask_strategy 'interrupt' needs cancel, which the {framework} adapter "
                "does not support"
            )
        if problems:
            raise RuntimeRefusal("capability_mismatch", "; ".join(problems), status=422)
        program = self.program_loader(manifest)
        runnable = adapter.build(manifest, program, self.store)
        with self._lock:
            self._hosted[version_id] = (caps, runnable, manifest)
        return caps, runnable, manifest

    # -- sessions (ADR 0144 d4, d7; ADR 0146 d4) ------------------------------------------------

    def _quota(self, tenant: str) -> dict[str, Any]:
        q = self._need_snap().get("quotas") or {}
        return {**(q.get("default") or {}), **((q.get("tenants") or {}).get(tenant) or {})}

    def usage(self, tenant: str) -> dict[str, Any]:
        """Sessions holding compute (``active``/``idle``) against the tenant's quota."""
        n = self.store.count_threads(tenant=tenant, statuses=("active", "idle"))
        return {
            "tenant": tenant,
            "active_sessions": n,
            "max_sessions": self._quota(tenant).get("max_sessions"),
        }

    def _cap(self, tenant: str) -> int | None:
        cap = self._quota(tenant).get("max_sessions")
        return None if cap is None else int(cap)

    def _admit(self, tenant: str) -> None:
        """A cheap early refusal. The binding check runs inside the write's own transaction
        (``create_thread`` / ``transition_thread``), so two requests cannot share a slot."""
        cap = self._cap(tenant)
        if (
            cap is not None
            and self.store.count_threads(tenant=tenant, statuses=HOLDING_STATUSES) >= cap
        ):
            raise RuntimeRefusal(
                "session_quota_exceeded",
                f"tenant {tenant!r} already holds {cap} active sessions",
                status=429,
            )

    def _reactivate(self, thread: dict[str, Any], tenant: str) -> None:
        """Move a session back to ``active``: a compare-and-set, under the tenant's quota.

        Never a blind write - a session closed or quarantined since it was read stays so (409 /
        423), and a suspended session that resumes takes its quota slot in the same transaction
        that checks the slot is free (429).
        """
        tid = thread["thread_id"]
        if self.store.transition_thread(
            tid,
            from_statuses=_RESUMABLE,
            to="active",
            max_active=self._cap(tenant),
            last_active_at=self.store.clock(),
        ):
            if thread["status"] == "suspended":
                self.store.log_event("session_resumed", tenant=tenant, thread_id=tid)
            return
        now = self.store.get_thread(tid, tenant=tenant) or {}
        if now.get("status") == "quarantined":
            raise RuntimeRefusal(
                "thread_quarantined",
                "the session's agent version was rolled back and quarantined",
                status=423,
            )
        raise RuntimeRefusal("thread_closed", "the session is closed", status=409)

    def open_session(
        self,
        agent: str,
        *,
        tenant: str,
        principal: str | None = None,
        alias: str = "Production",
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snap = self._need_snap()
        entry = snap["agents"].get(agent)
        if entry is None:
            raise RuntimeRefusal("unknown_agent", f"no agent named {agent!r}", status=404)
        vid = entry["aliases"].get(alias)
        if vid is None:
            raise RuntimeRefusal("no_version", f"{agent} has no {alias} version", status=404)
        self._admit(tenant)
        import uuid

        tid = thread_id or f"th-{uuid.uuid4().hex}"
        canary = False
        pct = float(entry.get("canary_percent") or 0)
        if alias == "Production" and "Canary" in entry["aliases"] and starts_on_canary(tid, pct):
            vid, canary = entry["aliases"]["Canary"], True
        self._host(vid)
        thread = self.store.create_thread(
            tenant=tenant,
            agent=agent,
            version_id=vid,
            principal=principal,
            canary=canary,
            metadata=metadata,
            thread_id=tid,
            max_active=self._cap(tenant),  # checked inside the insert's own transaction
        )
        self.store.log_event(
            "session_opened",
            tenant=tenant,
            thread_id=tid,
            detail={"agent": agent, "version_id": vid, "canary": canary},
        )
        return thread

    def _thread(self, thread_id: str, tenant: str) -> dict[str, Any]:
        t = self.store.get_thread(thread_id, tenant=tenant)
        if t is None:  # another tenant's thread is indistinguishable from a missing one
            raise RuntimeRefusal("not_found", f"no thread {thread_id}", status=404)
        return t

    def holder(self) -> str:
        """The lease holder: this worker AND this executing thread. Two request threads of one
        runtime process are two holders, so they cannot step one agent thread at once."""
        return f"{self.worker_id}#{threading.get_ident()}"

    def thread(self, thread_id: str, *, tenant: str) -> dict[str, Any]:
        """A thread of ``tenant``; another tenant's thread is reported as missing (404)."""
        return self._thread(thread_id, tenant)

    def close_session(self, thread_id: str, *, tenant: str) -> dict[str, Any]:
        t = self._thread(thread_id, tenant)
        for r in self.store.runs_for_thread(thread_id, statuses=ACTIVE_RUN_STATES):
            if r["status"] == "running":
                self.store.update_run(r["run_id"], stop_requested="cancel")
            else:
                self.store.update_run(r["run_id"], status="cancelled")
        self._release_sandbox(thread_id, "destroy")
        self.store.update_thread(thread_id, status="closed", last_active_at=self.store.clock())
        self.store.log_event("session_closed", tenant=t["tenant"], thread_id=thread_id)
        return self._thread(thread_id, tenant)

    def sweep(self, *, now: float | None = None) -> dict[str, list[str]]:
        """Move quiet sessions down the lifecycle; a suspended one releases its sandbox."""
        now = self.store.clock() if now is None else now
        out: dict[str, list[str]] = {"idle": [], "suspended": []}
        for status in ("active", "idle"):
            after = ""
            while True:
                page = self.store.list_threads(tenant=None, status=status, after=after, limit=500)
                for t in page:
                    busy = self.store.runs_for_thread(
                        t["thread_id"], statuses=("pending", "running")
                    )
                    if busy:
                        continue
                    quiet = now - float(t["last_active_at"])
                    # Compare-and-set on the status this page read: a session closed, resumed
                    # or quarantined meanwhile is never moved back down the lifecycle (a closed
                    # session turned `suspended` would be reopened by its next input).
                    if quiet >= self.suspend_after:
                        if not self.store.transition_thread(
                            t["thread_id"], from_statuses=(status,), to="suspended"
                        ):
                            continue
                        self._release_sandbox(t["thread_id"], "hibernate")
                        self.store.log_event(
                            "session_suspended", tenant=t["tenant"], thread_id=t["thread_id"]
                        )
                        out["suspended"].append(t["thread_id"])
                    elif status == "active" and quiet >= self.idle_after:
                        if self.store.transition_thread(
                            t["thread_id"], from_statuses=("active",), to="idle"
                        ):
                            out["idle"].append(t["thread_id"])
                if len(page) < 500:
                    break
                after = page[-1]["thread_id"]
        return out

    def owner(self, thread_id: str) -> str:
        """The worker that should serve this session (affinity on the runtime's own key)."""
        workers = sorted(set(self.peers) | {self.worker_id})
        return owner(thread_id, workers)

    # -- migrations and retirements --------------------------------------------------------------

    def _migrate(self, thread: dict[str, Any]) -> dict[str, Any]:
        entry = (self._snap or {}).get("agents", {}).get(thread["agent"], {})
        m = (entry.get("migrations") or {}).get(thread["version_id"])
        if not m or m.get("outcome") == "pin":
            return thread
        active = self.store.runs_for_thread(thread["thread_id"], statuses=ACTIVE_RUN_STATES)
        if m["outcome"] == "drain" and active:
            return thread  # in-flight work finishes on the old version first
        if m["outcome"] == "compatible":
            for r in active:
                if r["status"] != "running":
                    self.store.update_run(r["run_id"], version_id=m["to"])
        elif active:
            return thread
        self._host(m["to"])
        self.store.update_thread(thread["thread_id"], version_id=m["to"])
        self.store.log_event(
            "thread_migrated",
            tenant=thread["tenant"],
            thread_id=thread["thread_id"],
            detail={"from": thread["version_id"], "to": m["to"], "outcome": m["outcome"]},
        )
        return self.store.get_thread(thread["thread_id"]) or thread

    def _apply_retirements(self, doc: dict[str, Any]) -> None:
        for agent, entry in (doc.get("agents") or {}).items():
            for vid, policy in (entry.get("retired") or {}).items():
                key = f"{agent}:{vid}:{policy}"
                # Looked up in SQL, not in a page of events: once enough markers accumulate a
                # page stops containing the old ones and every snapshot would re-apply them.
                if policy == "continue" or self.store.has_event("retire_applied", key):
                    continue
                n = 0
                after = ""
                while True:
                    page = self.store.list_threads(
                        tenant=None, agent=agent, version_id=vid, after=after, limit=500
                    )
                    for t in page:
                        if t["status"] == "closed":
                            continue
                        for r in self.store.runs_for_thread(
                            t["thread_id"], statuses=ACTIVE_RUN_STATES
                        ):
                            if r["status"] == "running":
                                self.store.update_run(r["run_id"], stop_requested="cancel")
                            else:
                                self.store.update_run(r["run_id"], status="cancelled")
                        if policy == "quarantine":
                            self.store.transition_thread(
                                t["thread_id"], from_statuses=_RESUMABLE, to="quarantined"
                            )
                        n += 1
                    if len(page) < 500:
                        break
                    after = page[-1]["thread_id"]
                self.store.log_event(
                    "retire_applied", detail={"key": key, "threads": n, "policy": policy}
                )

    # -- runs (ADR 0144 d2, d3) ------------------------------------------------------------------

    def submit(self, thread_id: str, input: Any, *, tenant: str) -> dict[str, Any]:
        """Queue a run on a thread, applying the agent's multitask strategy if it is busy."""
        if len(json.dumps(input, default=str).encode()) > _MAX_INPUT_BYTES:
            raise RuntimeRefusal(
                "input_too_large", f"input exceeds {_MAX_INPUT_BYTES} bytes", status=413
            )
        t = self._thread(thread_id, tenant)
        self._reactivate(t, tenant)  # 409 closed · 423 quarantined · 429 quota on a resume
        t = self._migrate(t)
        manifest = self._manifest(t["version_id"])
        strategy = (manifest.get("policy") or {}).get("multitask_strategy") or _DEFAULT_STRATEGY
        rolled: list[dict[str, Any]] = []

        def decide(active: list[dict[str, Any]]) -> dict[str, str]:
            if strategy == "enqueue" and len(active) >= _MAX_QUEUED_PER_THREAD:
                raise RuntimeRefusal(
                    "thread_queue_full",
                    f"{len(active)} runs already wait on this thread "
                    f"(limit {_MAX_QUEUED_PER_THREAD})",
                    status=429,
                )
            if not active or strategy == "enqueue":
                return {}
            if strategy == "reject":
                raise RuntimeRefusal(
                    "thread_busy",
                    "the thread is busy and the agent rejects concurrent input",
                    status=409,
                )
            actions: dict[str, str] = {}
            for r in active:
                if r["status"] == "running":
                    actions[r["run_id"]] = f"stop:{strategy}"
                elif strategy == "interrupt":
                    actions[r["run_id"]] = "status:superseded"
                else:
                    actions[r["run_id"]] = "status:rolled_back"
                    rolled.append(r)
            return actions

        run, active = self.store.create_run(
            thread_id, tenant=tenant, version_id=t["version_id"], input=input, decide=decide
        )
        for r in rolled:  # settled in the transaction; discard their checkpoints now
            self.store.discard_after(thread_id, r.get("base_checkpoint_id"), run_id=r["run_id"])
        self.store.transition_thread(
            thread_id, from_statuses=_RESUMABLE, to="active", last_active_at=self.store.clock()
        )
        self.store.log_event(
            "run_submitted",
            tenant=tenant,
            thread_id=thread_id,
            run_id=run["run_id"],
            detail={"strategy": strategy, "busy": [r["run_id"] for r in active]},
        )
        return run

    def run_wait(self, thread_id: str, input: Any, *, tenant: str) -> dict[str, Any]:
        run = self.submit(thread_id, input, tenant=tenant)
        return self.execute(run["run_id"])

    def get_run(self, run_id: str, *, tenant: str) -> dict[str, Any]:
        r = self.store.get_run(run_id)
        if r is None or r["tenant"] != tenant:
            raise RuntimeRefusal("not_found", f"no run {run_id}", status=404)
        return r

    def cancel(self, run_id: str, *, tenant: str, action: str = "interrupt") -> dict[str, Any]:
        if action not in ("interrupt", "rollback", "cancel"):
            raise RuntimeRefusal(
                "bad_action", "action must be interrupt, rollback or cancel", status=400
            )
        r = self.get_run(run_id, tenant=tenant)
        if r["status"] == "running":
            self.store.update_run(run_id, stop_requested=action)
        elif r["status"] in ("pending", "interrupted"):
            status = {"interrupt": "superseded", "rollback": "rolled_back", "cancel": "cancelled"}[
                action
            ]
            if action == "rollback":
                self.store.discard_after(r["thread_id"], r.get("base_checkpoint_id"), run_id=run_id)
            self.store.update_run(run_id, status=status)
        return self.get_run(run_id, tenant=tenant)

    def execute(self, run_id: str) -> dict[str, Any]:
        """Execute a run - and whatever queued behind it - while holding the thread's lease.

        A thread another worker is stepping is left alone: that worker drains the queue.
        """
        run = self.store.get_run(run_id)
        if run is None:
            raise RuntimeRefusal("not_found", f"no run {run_id}", status=404)
        tid = run["thread_id"]
        if run["status"] not in ("pending", "running"):
            return run
        holder = self.holder()
        if not self.store.acquire_lease(tid, holder, self.lease_ttl):
            return run
        # The lease is kept alive for as long as this worker is stepping the thread - a single
        # node (a model call, a slow tool) may take longer than the lease TTL, and an expired
        # lease is exactly what lets another worker's recovery re-execute the node and repeat
        # its side effect. A dead worker stops renewing, so recovery still happens.
        beat = _LeaseHeartbeat(self.store, tid, holder, self.lease_ttl)
        beat.start()
        try:
            current: dict[str, Any] | None = run
            while current is not None:
                if current["status"] in ("pending", "running"):
                    if not self._execute_one(current, holder=holder):
                        break  # lease lost: the new owner drains the queue
                current = self._next_pending(tid)
        finally:
            beat.stop()
            self.store.release_lease(tid, holder)
        return self.store.get_run(run_id) or run

    def _next_pending(self, thread_id: str) -> dict[str, Any] | None:
        runs = self.store.runs_for_thread(thread_id, statuses=("pending", "interrupted"))
        if any(r["status"] == "interrupted" for r in runs):
            return None  # enqueued work waits behind a run parked on a human
        return runs[0] if runs else None

    def _resolve_models(self, manifest: dict[str, Any], version_id: str) -> dict[str, Any]:
        """Resolve every model binding ONCE, at run start (ADR 0146 d2)."""
        snap = self._need_snap()
        pins = (snap.get("reeval_pins") or {}).get(version_id, {})
        out: dict[str, Any] = {}
        for m in manifest.get("models", []):
            key = _servable_key(str(m["servable"]))
            if m["binding"] == "pin":
                out[m["role"]] = {
                    "servable": m["servable"],
                    "binding": "pin",
                    "version": str(m["version"]),
                }
                continue
            pin_key = f"{key}@{m['alias']}"
            if pin_key in pins:
                version, source = pins[pin_key], "reeval_pin"
            else:
                version, source = (snap.get("models") or {}).get(key, {}).get(m["alias"]), "alias"
            if version is None:
                raise RuntimeRefusal(
                    "unresolved_model",
                    f"model binding {m['role']!r} ({m['servable']}@{m['alias']}) has no version "
                    "in the snapshot",
                    status=503,
                )
            out[m["role"]] = {
                "servable": m["servable"],
                "binding": "follow",
                "alias": m["alias"],
                "version": str(version),
                "source": source,
            }
        return out

    def _execute_one(self, run: dict[str, Any], *, holder: str | None = None) -> bool:
        """Step one run; ``False`` when this worker lost the thread's lease mid-run (the run
        is left exactly as the new owner will find it)."""
        store = self.store
        rid, tid = run["run_id"], run["thread_id"]
        thread = store.get_thread(tid)
        if thread is None:
            return True
        if thread["status"] == "quarantined":
            store.update_run(rid, status="rejected", error="thread quarantined")
            return True
        try:
            caps, runnable, manifest = self._host(run["version_id"])
            if not run.get("resolved_models"):
                resolved = self._resolve_models(manifest, run["version_id"])
                store.update_run(rid, resolved_models=resolved)
                run["resolved_models"] = resolved
        except RuntimeRefusal as exc:
            store.update_run(rid, status="rejected", error=f"{exc.code}: {exc.reason}")
            store.log_event(
                "run_rejected",
                tenant=run["tenant"],
                thread_id=tid,
                run_id=rid,
                detail=exc.as_dict(),
            )
            return True
        started = run.get("base_checkpoint_id") is not None
        if not started:
            latest = store.latest_checkpoint(tid)
            base = latest["checkpoint_id"] if latest and latest["run_id"] != rid else 0
            store.update_run(rid, base_checkpoint_id=base)
            run["base_checkpoint_id"] = base
        store.update_run(rid, status="running")
        hooks = _Hooks(self, thread, run, manifest, holder=holder)
        hooks.continuing = started
        resolved_intr = [
            i
            for i in store.interrupts_for_thread(tid)
            if i["run_id"] == rid and i["status"] == "resolved"
        ]
        try:
            if resolved_intr:
                hooks.resume_value = resolved_intr[-1].get("value")
                for i in resolved_intr:
                    self.store.consume_interrupt(i["key"])
                result = runnable.resume(thread, hooks)
            else:
                result = runnable.invoke(thread, run.get("input"), hooks)
        except LeaseLost:
            store.log_event(
                "run_lease_lost",
                tenant=run["tenant"],
                thread_id=tid,
                run_id=rid,
                detail={"worker": self.worker_id},
            )
            return False
        except Interrupt as i:
            store.put_interrupt(i.key, run_id=rid, thread_id=tid, kind=i.kind, payload=i.payload)
            store.update_run(rid, status="interrupted")
            store.log_event(
                "run_interrupted",
                tenant=run["tenant"],
                thread_id=tid,
                run_id=rid,
                detail={"kind": i.kind, "key": i.key},
            )
            return True
        except Exception as exc:  # noqa: BLE001 - an agent failing is a run outcome, not a crash
            store.update_run(rid, status="error", error=f"{type(exc).__name__}: {exc}"[:2000])
            store.log_event(
                "run_failed",
                tenant=run["tenant"],
                thread_id=tid,
                run_id=rid,
                detail={"error": type(exc).__name__},
            )
            return True
        self._settle(run, result)
        store.update_thread(tid, last_active_at=store.clock())
        return True

    def _settle(self, run: dict[str, Any], result: RunResult) -> None:
        store = self.store
        rid, tid = run["run_id"], run["thread_id"]
        status = result.status
        if status == "success":
            store.update_run(rid, status="success", output=result.output)
        elif status == "interrupted":  # adapter-native interrupt (LangGraph)
            n = len([i for i in store.interrupts_for_thread(tid) if i["run_id"] == rid])
            key = f"lg:{rid}:{n}"
            intr = result.interrupt or {}
            store.put_interrupt(
                key,
                run_id=rid,
                thread_id=tid,
                kind=intr.get("kind", "input"),
                payload={"value": intr.get("payload")},
            )
            store.update_run(rid, status="interrupted")
        elif status == "stop:interrupt":
            store.update_run(rid, status="superseded")
        elif status == "stop:rollback":
            store.discard_after(tid, run.get("base_checkpoint_id"), run_id=rid)
            store.update_run(rid, status="rolled_back")
        elif status == "stop:budget":
            store.update_run(rid, status="budget_exceeded", error="max_steps reached")
        elif status.startswith("stop:"):
            store.update_run(rid, status="cancelled")
        else:
            store.update_run(rid, status="error", error=result.error or status)
        store.log_event(
            "run_finished",
            tenant=run["tenant"],
            thread_id=tid,
            run_id=rid,
            detail={"status": status, "steps": result.steps},
        )

    def resume(
        self, thread_id: str, value: Any, *, tenant: str, by: str | None = None
    ) -> dict[str, Any]:
        """Answer the interrupt a thread is parked on, then continue the run.

        For an approval, ``value`` is ``{"approve": true|false}``. The answer is stored before
        the run continues, so a restart between the two loses nothing; a second answer to the
        same interrupt is refused (the first one stands).
        """
        t = self._thread(thread_id, tenant)
        runs = self.store.runs_for_thread(thread_id, statuses=("interrupted",))
        if not runs:
            raise RuntimeRefusal("no_interrupt", "the thread is not waiting on anyone", status=409)
        run = runs[-1]
        intr = self.store.open_interrupt(run["run_id"])
        if intr is None:
            raise RuntimeRefusal("no_interrupt", "the run has no open interrupt", status=409)
        if intr["kind"] == "approval" and not (
            isinstance(value, dict) and isinstance(value.get("approve"), bool)
        ):
            raise RuntimeRefusal(
                "bad_resume", 'an approval needs {"approve": true|false}', status=400
            )
        # A session parked on a human is swept to `suspended` like any quiet one; answering it
        # resumes the session, which takes a quota slot like any other resume.
        self._reactivate(t, tenant)
        if not self.store.resolve_interrupt(intr["key"], value, by=by):
            raise RuntimeRefusal(
                "already_resolved", "this interrupt was already answered", status=409
            )
        self.store.update_run(run["run_id"], status="pending")
        self.store.log_event(
            "interrupt_resolved",
            tenant=tenant,
            thread_id=thread_id,
            run_id=run["run_id"],
            detail={"kind": intr["kind"], "by": by},
        )
        return self.execute(run["run_id"])

    def recover(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Resume runs whose worker died: status ``running`` with no live lease.

        At most ``limit`` runs per call - the orphan test (no live lease) is applied in SQL
        before that limit, so live runs can never crowd an orphan out of the window.
        """
        out = []
        for r in self.store.orphaned_runs(now=self.store.clock(), limit=limit):
            self.store.log_event(
                "run_recovered",
                tenant=r["tenant"],
                thread_id=r["thread_id"],
                run_id=r["run_id"],
                detail={"worker": self.worker_id},
            )
            out.append(self.execute(r["run_id"]))
        return out

    def get_state(self, thread_id: str, *, tenant: str) -> StateSnapshot:
        t = self._thread(thread_id, tenant)
        _, runnable, _ = self._host(t["version_id"])
        return runnable.get_state(t)

    # -- sandbox (ADR 0145 d6) --------------------------------------------------------------------

    def _sandbox_exec(
        self,
        thread: dict[str, Any],
        manifest: dict[str, Any],
        command: str,
        *,
        timeout: float | None,
    ) -> dict[str, Any]:
        tid = thread["thread_id"]
        sb = manifest.get("sandbox") or {}
        tenant_iso = self._quota(thread["tenant"]).get("sandbox_isolation")
        required = stricter(sb.get("isolation"), tenant_iso)
        try:
            with self._lock:
                held = self._sandboxes.get(tid)
            if held is None or held[1].state == "destroyed":
                provider = select_provider(self.sandbox_providers, required=required)
                handle = provider.claim(
                    tid.replace(":", "-"),
                    sb.get("template") or "python-3.12-min",
                    sb.get("egress") or (),
                )
                with self._lock:
                    self._sandboxes[tid] = (provider, handle)
                self.store.log_event(
                    "sandbox_claimed",
                    tenant=thread["tenant"],
                    thread_id=tid,
                    detail={"provider": provider.name, "isolation": handle.isolation},
                )
            else:
                provider, handle = held
                if handle.state == "hibernated" and hasattr(provider, "resume"):
                    provider.resume(handle)
            return provider.exec(handle, command, timeout=timeout)
        except SandboxRefused as exc:
            self.store.log_event(
                "sandbox_refused",
                tenant=thread["tenant"],
                thread_id=tid,
                detail={"code": exc.code, "reason": exc.reason},
            )
            return {"ok": False, "code": exc.code, "error": exc.reason}

    def _release_sandbox(self, thread_id: str, mode: str) -> None:
        with self._lock:
            held = self._sandboxes.get(thread_id)
            if held is not None and mode == "destroy":
                self._sandboxes.pop(thread_id, None)
        if held is None:
            return
        provider, handle = held
        try:
            provider.release(handle, mode)
        except SandboxRefused as exc:
            logger.warning("sandbox release for %s failed: %s", thread_id, exc)

    # -- replay shadow (ADR 0146 d4) --------------------------------------------------------------

    def replay(self, thread_id: str, candidate_version_id: str, *, tenant: str) -> dict[str, Any]:
        """Run ``candidate_version_id`` over a recorded session with tools stubbed from it.

        The candidate gets a :class:`_ReplayGateway` - it has no route to a live tool - on a
        scratch thread that is deleted afterwards. Recorded human answers are replayed in order.
        """
        src = self._thread(thread_id, tenant)
        runs = self.store.runs_for_thread(thread_id, statuses=("success",))
        recordings: list[dict[str, Any]] = []
        for r in runs:
            recordings.extend(self.store.tool_calls_for_run(r["run_id"]))
        answers = [
            i.get("value")
            for i in self.store.interrupts_for_thread(thread_id)
            if i["kind"] == "input" and i["status"] != "open"
        ]
        stub = _ReplayGateway(recordings)
        child = AgentRuntime(
            self.store,
            gateway=stub,
            program_loader=self.program_loader,
            worker_id=f"{self.worker_id}-replay",
            require_durable=False,
            sandbox_providers=(),
        )
        child._snap = self._snap
        self._manifest(candidate_version_id)
        scratch = self.store.create_thread(
            tenant=tenant,
            agent=src["agent"],
            version_id=candidate_version_id,
            principal=src.get("principal"),
            metadata={"replay_of": thread_id},
        )
        report: dict[str, Any] = {
            "thread_id": thread_id,
            "candidate": candidate_version_id,
            "runs": [],
        }
        try:
            for r in runs:
                got = child.run_wait(scratch["thread_id"], r["input"], tenant=tenant)
                while got["status"] == "interrupted" and answers:
                    intr = self.store.open_interrupt(got["run_id"])
                    if intr is None or intr["kind"] != "input":
                        break
                    got = child.resume(
                        scratch["thread_id"], answers.pop(0), tenant=tenant, by="replay"
                    )
                report["runs"].append(
                    {
                        "recorded_run": r["run_id"],
                        "status": got["status"],
                        "recorded_output": r.get("output"),
                        "candidate_output": got.get("output"),
                        "equal": got.get("output") == r.get("output"),
                    }
                )
        finally:
            self.store.delete_thread(scratch["thread_id"])
        report.update(
            tool_calls=stub.calls,
            matched=stub.matched,
            not_recorded=stub.unmatched,
            live_calls=0,
            equal_outputs=sum(1 for x in report["runs"] if x["equal"]),
        )
        return report


class _LeaseHeartbeat:
    """Renews one thread's lease every ``ttl / 3`` seconds while a worker steps it.

    Stops by itself when the renewal is refused (another worker holds the lease - the hooks
    then raise ``LeaseLost`` at the next tool call or checkpoint). A store error is logged and
    retried on the next beat: the lease still has up to two thirds of its TTL left.
    """

    def __init__(self, store: AgentStateStore, thread_id: str, holder: str, ttl: float) -> None:
        self.store = store
        self.thread_id = thread_id
        self.holder = holder
        self.ttl = float(ttl)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-{self.thread_id[:16]}", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        period = max(self.ttl / 3.0, 0.01)
        while not self._stop.wait(period):
            try:
                if not self.store.acquire_lease(self.thread_id, self.holder, self.ttl):
                    return
            except Exception as exc:  # noqa: BLE001 - retried on the next beat
                logger.warning("lease renewal for %s failed: %s", self.thread_id, exc)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self.ttl, 1.0))
            self._thread = None


class _ReplayGateway:
    """Serves recorded tool results by ``(tool, argument digest)``, in recorded order.

    Holds no reference to any live gateway, so nothing a candidate does in replay can reach a
    real tool (ADR 0146 verification 5). An unrecorded call is refused and reported.
    """

    def __init__(self, recordings: list[dict[str, Any]]) -> None:
        self._queues: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for rec in recordings:
            self._queues.setdefault((rec["tool"], rec["args_digest"]), []).append(rec["result"])
        self.calls = 0
        self.matched = 0
        self.unmatched: list[dict[str, Any]] = []

    def call(
        self,
        caller: ToolCaller,
        tool: str,
        args: dict[str, Any],
        *,
        approved: bool,
        idempotency_key: str,
        tenant: str,
    ) -> dict[str, Any]:
        self.calls += 1
        q = self._queues.get((tool, _digest(args)))
        if q:
            self.matched += 1
            return copy.deepcopy(q.pop(0))
        self.unmatched.append({"tool": tool, "args_digest": _digest(args)})
        return {
            "ok": False,
            "code": "not_recorded",
            "error": f"replay: no recorded result for {tool}",
        }
