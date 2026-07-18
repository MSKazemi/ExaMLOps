"""``exa events`` — NovaFabric event backbone: transactional-outbox relay + stats (item 1.3).

Operate the publish side of the event backbone: run the relay (from cron / a sidecar) to drain
the outbox to the configured broker, inspect backlog, or hand-publish an event.
"""

from __future__ import annotations

import json

import typer

from examlops.cli import _output

app = typer.Typer(no_args_is_help=True, help="NovaFabric event backbone (transactional outbox)")


@app.command("relay")
def relay(
    limit: int = typer.Option(100, "--limit", "-n", help="Max events to publish this pass"),
    loop: bool = typer.Option(False, "--loop", help="Keep relaying until the outbox is drained"),
) -> None:
    """Publish pending outbox events to the configured broker (EXAMLOPS_EVENT_PUBLISHER)."""
    from examlops import events

    total = {"claimed": 0, "published": 0, "failed": 0}
    while True:
        r = events.relay_once(limit)
        for k in total:
            total[k] += r[k]
        if not loop or r["claimed"] == 0:
            break
    if _output.json_mode:
        _output.print_json(total)
        return
    _output.ok(
        f"Relayed: {total['published']} published, {total['failed']} failed "
        f"({total['claimed']} claimed)."
    )


@app.command("stats")
def stats() -> None:
    """Show outbox backlog: pending / published / poison (attempts exhausted)."""
    from examlops.data import init_db
    from examlops.data.events import outbox_stats

    init_db()
    s = outbox_stats()
    if _output.json_mode:
        _output.print_json(s)
        return
    _output.print_table(
        "Event outbox",
        ["Metric", "Count"],
        [
            ["pending", str(s["pending"])],
            ["published", str(s["published"])],
            ["poison", str(s["poison"])],
        ],
    )


@app.command("publish")
def publish(
    topic: str = typer.Argument(..., help="Event topic, e.g. drift.detected"),
    payload: str = typer.Option("{}", "--payload", "-p", help="JSON payload"),
) -> None:
    """Enqueue an event to the outbox (durable; relayed by `exa events relay`)."""
    from examlops import events

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        _output.error(f"--payload must be valid JSON: {exc}")
        raise typer.Exit(1) from exc
    event_id = events.publish(topic, data)
    if _output.json_mode:
        _output.print_json({"id": event_id, "topic": topic})
        return
    _output.ok(f"Enqueued event #{event_id} on '{topic}'.")
