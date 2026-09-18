"""An audit event that was never written must not vanish without a trace.

Forty-three call sites wrap their audit write in ``except Exception: pass``. The **policy** is
right and is not what these tests change: a promotion must not fail because the audit datastore is
briefly unreachable, and a secret rotation must not be blocked by it either. The defect is that the
loss is *invisible*, and the platform has exactly two mechanisms for trusting its audit log, both of
which are blind to it by construction:

* **The hash chain** (``exa audit verify``) proves **integrity, never completeness**. An event that
  never arrived leaves a perfectly valid chain — there is no gap to detect, because the chain is
  built from the rows that exist. (The related lesson was already learned on 2026-09-02 for rows
  that are present but *unchained*; those are now counted rather than skipped. A row that never
  arrived cannot even be counted.)
* **The EU-AI-Act Art. 12 coverage report** (``check_art12_logging``) asks only whether *at least
  one* event of each required type exists. So a dropped ``eval_gate_override`` still reports full
  coverage as long as some other override was recorded — and ``evaluation/gate.py`` is one of the
  forty-three.

So the fix is the one the gateway's accounting failures already use in this codebase: keep failing
open, but do it **loudly and countably**.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from examlops.data import audit


@pytest.fixture(autouse=True)
def _clean_counters():
    audit.reset_dropped_audit_events()
    yield
    audit.reset_dropped_audit_events()


def _make_write_fail(monkeypatch, exc=RuntimeError("datastore unreachable")):
    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(audit, "write_audit_event", boom)


def test_a_dropped_event_is_logged_with_its_cause(monkeypatch, caplog):
    _make_write_fail(monkeypatch)
    with caplog.at_level(logging.WARNING):
        ok = audit.audit_best_effort("exa-eval", "alice", "eval_gate_override", "JPCP", {})
    assert ok is False, "the helper reports the loss to a caller that wants to know"
    assert caplog.records, "a dropped audit event produced no log line at all"
    text = caplog.text
    assert "eval_gate_override" in text, "the log must name the action that went unrecorded"
    assert "datastore unreachable" in text, "the log must carry the cause"


def test_dropped_events_are_counted_per_action(monkeypatch):
    _make_write_fail(monkeypatch)
    audit.audit_best_effort("exa-eval", "a", "eval_gate_override", "JPCP", {})
    audit.audit_best_effort("exa-eval", "a", "eval_gate_override", "MACK", {})
    audit.audit_best_effort("exa-retrain", "a", "retrain_triggered", "JPCP", {})
    assert audit.dropped_audit_events() == {
        "eval_gate_override": 2,
        "retrain_triggered": 1,
    }


def test_the_write_still_fails_open(monkeypatch):
    """The caller's operation must survive the loss — that policy is deliberate and unchanged.

    Stated as a value rather than as "it did not raise", so the test has a claim of its own: a
    caller that ignores the return value carries on, and one that checks it is told the truth.
    """
    _make_write_fail(monkeypatch)
    reached = []
    ok = audit.audit_best_effort("exa-secrets", "a", "secret_rotated", "db/password", {})
    reached.append("the caller kept going")
    assert ok is False
    assert reached == ["the caller kept going"]
    assert audit.dropped_audit_events() == {"secret_rotated": 1}


def test_a_successful_write_is_neither_logged_nor_counted(monkeypatch, caplog):
    """Anti-vacuity: the counter must react to failure, not to being called."""
    calls: list[tuple] = []
    monkeypatch.setattr(audit, "write_audit_event", lambda *a, **k: calls.append((a, k)))
    with caplog.at_level(logging.WARNING):
        ok = audit.audit_best_effort("exa-eval", "a", "eval_gate_override", "JPCP", {"x": 1})
    assert ok is True
    assert calls, "the helper must actually attempt the write"
    assert audit.dropped_audit_events() == {}
    assert not caplog.records


def test_keyword_arguments_reach_the_underlying_write(monkeypatch):
    """`tenant` and `conn` decide which log the event lands in; dropping them would misfile it."""
    seen: dict = {}
    monkeypatch.setattr(
        audit, "write_audit_event", lambda *a, **k: seen.update({"args": a, "kwargs": k})
    )
    sentinel = object()
    audit.audit_best_effort(
        "exa-policy",
        "a",
        "policy_decided",
        "t/v1",
        {"effect": "deny"},
        tenant="acme",
        conn=sentinel,
    )
    assert seen["args"] == ("exa-policy", "a", "policy_decided", "t/v1", {"effect": "deny"})
    assert seen["kwargs"] == {"tenant": "acme", "conn": sentinel}


#: Every action string that satisfies an `ART12_REQUIRED_EVENTS` prefix-match, i.e. the events
#: `check_art12_logging` counts. Written out because the required list is a substring match
#: (`action LIKE %approval%`) and the guard must know the concrete action names in the tree.
ART12_ACTIONS = (
    "retrain_triggered",
    "drift_auto_retrain_triggered",
    "autopilot_retrain_triggered",
    "promotion",
    "model_promoted",
    "autopilot_promoted",
    "eval_gate_override",
    "approval",
    "model_approved",
    "model_rejected",
)
#: Calls that reach the datastore themselves, so a failure raises out of them.
RAW_WRITERS = {"write_audit_event", "append_audit_event"}
#: Those plus the local wrappers around them. A wrapper may or may not absorb the failure, which is
#: why the two sweeps below use different sets: "did this record an Art. 12 event" is a question
#: about any of them, "can this raise inside a handler" is a question about the raw writers only.
AUDIT_WRITERS = RAW_WRITERS | {"audit", "_audit", "_write_audit"}
#: Callables that *run* another callable. The dashboard writes its audit events as
#: `await asyncio.to_thread(audit_write.audit, …)` — the writer is an argument, not the callee — so
#: a sweep that only reads `call.func` cannot see a single dashboard audit write.
DISPATCHERS = {"to_thread", "run_in_executor", "submit", "apply_async", "run_sync", "create_task"}
RUNTIME_ROOTS = (
    "platform/cli/src/examlops",
    "platform/services",
    "platform/clients",
    "pipelines",
    "serving",
)


def _string_constants(node) -> set[str]:
    """Every string literal anywhere inside *node*.

    Walking the whole node rather than reading `call.args[2]` is what makes the sweep see an action
    name that is not a bare literal. Measured against five spellings: a literal, a keyword argument,
    a conditional (`"model_promoted" if x else "model_alias_set"` — the form `routers/models.py`
    actually uses for an Art. 12 event), an f-string, and a name bound to a literal. The first
    version of this detector saw only two of the five.
    """
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _literal_names(tree) -> dict[str, str]:
    """Names bound to a plain string literal anywhere in the module (`ACTION = "promotion"`)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = node.value.value
    return out


#: Attribute names that mean "this handler said something". `log.error(...)` is as common as
#: `logger.warning(...)` and a substring check for "logger" misses it — which is how the SeanerBUS
#: bridge was reported as silent when it logs on every line of its handler.
LOG_CALLS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _handler_logs(handler) -> bool:
    """Whether the handler reports the failure somewhere an operator could find it."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fn in LOG_CALLS:
                return True
    return False


def _is_audit_write(call, writers: set[str] = AUDIT_WRITERS) -> bool:
    """Whether this call performs an audit write — directly, or by handing one to a dispatcher."""
    name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
    if name in writers:
        return True
    if name in DISPATCHERS and call.args:
        first = call.args[0]
        ref = getattr(first, "id", None) or getattr(first, "attr", None)
        return ref in writers
    return False


def _art12_events_in(call, names: dict[str, str]) -> set[str]:
    """The Art. 12 action names this audit-write call can be recording."""
    found = _string_constants(call) & set(ART12_ACTIONS)
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        if isinstance(arg, ast.Name) and names.get(arg.id) in ART12_ACTIONS:
            found.add(names[arg.id])
    return found


def _runtime_files(root):
    return [
        f
        for r in RUNTIME_ROOTS
        for f in sorted((root / r).rglob("*.py"))
        if "/tests/" not in str(f) and "/test_" not in str(f) and ".superpowers" not in str(f)
    ]


def test_the_art12_required_events_are_written_through_the_helper():
    """No Art. 12 event may be recorded by a call site that swallows a failed write silently.

    **This list is derived, not hand-written, and that is the whole point.** The first version of
    this guard named four files I had already fixed, so it passed while the *agent* wrote
    `retrain_triggered` through a silent handler and I claimed all five types were covered. A guard
    whose scope is a list of the things you fixed cannot tell you what you missed.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    files = _runtime_files(root)
    assert len(files) > 200, (
        f"the sweep found only {len(files)} files — it is not reaching the tree"
    )

    offenders = []
    for path in files:
        tree = ast.parse(path.read_text())
        names = _literal_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            events = set()
            for stmt in node.body:  # the BODY: what this handler actually protects
                for call in ast.walk(stmt):
                    if not isinstance(call, ast.Call):
                        continue
                    if not _is_audit_write(call):
                        continue
                    events |= _art12_events_in(call, names)
            if not events:
                continue
            for handler in node.handlers:
                t = handler.type
                broad = t is None or (isinstance(t, ast.Name) and t.id == "Exception")
                if not broad or any(isinstance(x, ast.Raise) for x in ast.walk(handler)):
                    continue
                if not _handler_logs(handler):
                    rel = path.relative_to(root).as_posix()
                    offenders.append(f"{rel}:{handler.lineno} {sorted(events)}")
    assert not offenders, (
        "these record an Art. 12 required event and swallow a failed write silently:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `audit.audit_best_effort(...)` — it still fails open, but logs and counts."
    )


def test_the_art12_detector_finds_a_planted_offender():
    """Anti-vacuity: a derived sweep that matches nothing passes forever."""
    planted = (
        "def f():\n"
        "    try:\n"
        "        write_audit_event('cli', a, 'retrain_triggered', m, {})\n"
        "    except Exception:\n"
        "        pass\n"
    )
    found = []
    for node in ast.walk(ast.parse(planted)):
        if not isinstance(node, ast.Try):
            continue
        for stmt in node.body:
            for call in ast.walk(stmt):
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                    if name in AUDIT_WRITERS:
                        for arg in call.args:
                            if isinstance(arg, ast.Constant) and arg.value in ART12_ACTIONS:
                                found.append(arg.value)
    assert found == ["retrain_triggered"], f"the detector did not see the planted write: {found}"


# ── the inverse failure: an audit write *inside* an error handler ─────────────


def test_a_failed_audit_write_does_not_take_down_the_autopilot_loop(monkeypatch):
    """`_classify_anomaly_for` promises a broken detector cannot take the loop down.

    The line below that promise wrote the failure to the audit trail **unprotected**, inside the
    very handler that was absorbing the error. `run_cycle`'s only outer handler catches
    `_RunKilled`, not `Exception` — so an audit datastore that was away turned one model's handled
    classification failure into an escape from the whole cycle, with the run row never updated.
    That is the exact inverse of a dropped event: the same outage, the opposite blast radius.
    """
    from examlops.cli.commands import autopilot_cmd

    def detector_fails(*_a, **_k):
        raise ValueError("corruption detector is broken")

    def audit_fails(*_a, **_k):
        raise RuntimeError("datastore unreachable")

    monkeypatch.setattr("examlops.corruption.assess_model", detector_fails)
    monkeypatch.setattr(autopilot_cmd, "write_audit_event", audit_fails, raising=False)
    monkeypatch.setattr("examlops.data.audit.write_audit_event", audit_fails, raising=False)

    # The promise in the docstring: `None`, not an exception.
    assert _classify(autopilot_cmd, "JPCP", {"z_score": 4.0}) is None


def _classify(mod, model, signal):
    return mod._classify_anomaly_for(model, signal)


def test_no_audit_write_inside_an_exception_handler_can_raise():
    """An audit write in a handler must use the never-raising helper.

    A write in a `try` body that fails loses a record. A write in an *except* body that fails
    replaces the error being handled with its own — converting a contained failure into an
    uncontained one. `audit_best_effort` cannot raise, which is why it is the only form allowed
    there.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    roots = (
        "platform/cli/src/examlops",
        "platform/services",
        "platform/clients",
        "pipelines",
        "serving",
    )
    files = [
        f
        for r in roots
        for f in sorted((root / r).rglob("*.py"))
        if "/tests/" not in str(f) and "/test_" not in str(f) and ".superpowers" not in str(f)
    ]
    assert len(files) > 200, (
        f"the sweep found only {len(files)} files — it is not reaching the tree"
    )
    offenders = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                for call in ast.walk(handler):
                    if not isinstance(call, ast.Call):
                        continue
                    # RAW_WRITERS only: a local `_audit(...)` wrapper may absorb the failure
                    # itself, and this guard is about calls that can *raise* inside a handler.
                    # Dispatcher-aware, because the dashboard hands its writer to `to_thread`.
                    if _is_audit_write(call, RAW_WRITERS):
                        offenders.append(f"{rel}:{call.lineno}")
    assert not offenders, (
        "an unprotected audit write inside an exception handler — if it raises, it replaces the "
        "error being handled:\n  " + "\n  ".join(offenders) + "\n\nUse `audit_best_effort`."
    )


@pytest.mark.parametrize(
    ("shape", "source"),
    [
        ("literal", 'write_audit_event("cli", a, "model_promoted", m, {})'),
        ("keyword", 'write_audit_event("cli", a, action="model_promoted", target=m)'),
        # The form `routers/models.py` actually uses for an Art. 12 event.
        (
            "conditional",
            'write_audit_event("cli", a, "model_promoted" if x else "alias_set", m, {})',
        ),
        ("f-string", 'write_audit_event("cli", a, f"model_promoted", m, {})'),
        ("name bound to a literal", "write_audit_event('cli', a, ACTION, m, {})"),
    ],
)
def test_the_detector_sees_every_spelling_of_an_action_name(shape, source):
    """The detector must not depend on how the action name is written.

    Its first version matched `ast.Constant` in the argument list only, so it saw the literal and
    the keyword form and was blind to the other three — including the conditional that
    `routers/models.py` uses for `model_promoted`. A detector that misses three spellings in five
    reports zero offenders and reads exactly like a clean tree.
    """
    module = f'ACTION = "model_promoted"\ntry:\n    {source}\nexcept Exception:\n    pass\n'
    tree = ast.parse(module)
    names = _literal_names(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                for call in ast.walk(stmt):
                    if isinstance(call, ast.Call):
                        fn = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                        if fn in AUDIT_WRITERS:
                            found |= _art12_events_in(call, names)
    assert "model_promoted" in found, f"the {shape} spelling is invisible to the detector"


def test_widening_the_walk_costs_only_an_exact_collision():
    """What the wider walk does and does not cost, measured rather than assumed.

    Walking the whole call for string literals could in principle match a *details* value instead
    of the action. In practice the match is exact, so prose that merely contains an action word
    ("promotion plan") does not collide — I predicted it would and it does not. The real cost is
    narrower: a details value equal to an action name, which is recorded here rather than left as
    a surprise. Over-reporting is the safe direction for this guard — it asks for a helper that
    still fails open.
    """

    def events(module: str) -> set[str]:
        tree = ast.parse(module)
        names = _literal_names(tree)
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for stmt in node.body:
                    for call in ast.walk(stmt):
                        if isinstance(call, ast.Call):
                            fn = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                            if fn in AUDIT_WRITERS:
                                found |= _art12_events_in(call, names)
        return found

    near_miss = (
        'try:\n    write_audit_event("cli", a, "traffic_changed", m, {"note": "promotion plan"})\n'
        "except Exception:\n    pass\n"
    )
    assert events(near_miss) == set(), "prose containing an action word must not match"

    exact = (
        'try:\n    write_audit_event("cli", a, "traffic_changed", m, {"kind": "promotion"})\n'
        "except Exception:\n    pass\n"
    )
    assert events(exact) == {"promotion"}, "the known, accepted cost: an exact collision matches"


def _catches_everything(handler) -> bool:
    t = handler.type
    if t is None or (isinstance(t, ast.Name) and t.id == "Exception"):
        return True
    return isinstance(t, ast.Tuple) and any(getattr(e, "id", None) == "Exception" for e in t.elts)


def _unprotected_writes_in_loops(tree) -> list[tuple[str, int]]:
    """Raw audit writes anywhere in a **batch** function — one that audits inside a loop.

    The scope is **structural, not a list of files**: a raw write inside a loop is the shape where
    a transient audit failure stops work part-way. A one-shot command that dies on an audit error
    shows the operator a traceback, which is arguably the right outcome; a loop that dies has
    already acted on some items, will not act on the rest, and — being autonomous — has nobody
    watching.
    """
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        protected: set[int] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Try) and any(_catches_everything(h) for h in node.handlers):
                for stmt in node.body:
                    for n in ast.walk(stmt):
                        line = getattr(n, "lineno", None)
                        if line is not None:
                            protected.add(line)
        # A function that audits inside a loop is a **batch** operation, and that makes every one
        # of its audit writes unsafe, not only the ones in the loop. `run_cycle` proved why: the
        # `autopilot_cycle_complete` write sits *after* the loops, so a failure there discarded the
        # whole cycle's result — the telemetry anchor and the event-backbone publish below it never
        # ran, and the caller got a traceback instead of the summary. Both of those neighbours are
        # commented "best-effort: must never fail the cycle"; the audit line between them was not.
        audits_in_loop = any(
            isinstance(n, ast.Call) and _is_audit_write(n, RAW_WRITERS | {"audit_best_effort"})
            for loop in ast.walk(fn)
            if isinstance(loop, ast.For | ast.AsyncFor)
            for n in ast.walk(loop)
        )
        if not audits_in_loop:
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and _is_audit_write(node, RAW_WRITERS)
                and node.lineno not in protected
            ):
                found.append((fn.name, node.lineno))
    return sorted(set(found))


def test_the_loop_detector_sees_a_planted_write_and_not_a_protected_one():
    """Anti-vacuity, both directions — the detector must distinguish protected from not."""
    unprotected = (
        "def cycle():\n"
        "    for m in models:\n"
        "        act(m)\n"
        "        write_audit_event('autopilot', a, 'promotion', m, {})\n"
    )
    assert _unprotected_writes_in_loops(ast.parse(unprotected)) == [("cycle", 4)]

    protected = (
        "def cycle():\n"
        "    for m in models:\n"
        "        try:\n"
        "            write_audit_event('autopilot', a, 'promotion', m, {})\n"
        "        except Exception:\n"
        "            log.warning('lost')\n"
    )
    assert _unprotected_writes_in_loops(ast.parse(protected)) == []

    outside_a_loop = "def once():\n    write_audit_event('cli', a, 'promotion', m, {})\n"
    assert _unprotected_writes_in_loops(ast.parse(outside_a_loop)) == []


def test_an_audit_failure_cannot_end_a_loop_part_way():
    """No autonomous or batch loop may be stopped by a transient audit-datastore failure.

    Found by measuring rather than reading: `run_cycle` alone held seven, each one a point where
    an unreachable datastore ended the self-driving cycle with earlier models already retrained or
    promoted — and `update_autopilot_run()`, the bookkeeping at the end, never ran, so the run row
    stayed incomplete about actions that had really happened.
    """
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for path in _runtime_files(root):
        for fn_name, line in _unprotected_writes_in_loops(ast.parse(path.read_text())):
            offenders.append(f"{path.relative_to(root).as_posix()}:{line} in {fn_name}()")
    assert not offenders, (
        "a failed audit write here ends the loop, leaving earlier items acted on and later ones "
        "untouched:\n  " + "\n  ".join(offenders) + "\n\nUse `audit_best_effort` — it cannot raise."
    )
