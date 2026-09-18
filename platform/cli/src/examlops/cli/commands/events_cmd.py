"""``exa events`` — NovaFabric event backbone: transactional-outbox relay + stats (item 1.3).

Operate the publish side of the event backbone: run the relay (from cron / a sidecar) to drain
the outbox to the configured broker, inspect backlog, or hand-publish an event.
"""

from __future__ import annotations

import json
import os
from typing import Any

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

    total: dict[str, Any] = {"claimed": 0, "published": 0, "failed": 0, "deferred": 0}
    unavailable: str | None = None
    while True:
        r = events.relay_once(limit)
        for k in total:
            total[k] += r.get(k, 0)
        unavailable = r.get("unavailable")
        # A deferred batch claimed rows it could not send: without this, `--loop` would spin on
        # an absent broker forever, re-deferring the same backlog.
        if not loop or r["claimed"] == 0 or unavailable:
            break
    if unavailable:
        total["unavailable"] = unavailable
    if _output.json_mode:
        _output.print_json(total)
        return
    if unavailable:
        _output.warning(
            f"Event backbone unavailable ({unavailable}). {total['deferred']} event(s) stay in "
            f"the outbox and go out when it is back; {total['published']} published first."
        )
        return
    _output.ok(
        f"Relayed: {total['published']} published, {total['failed']} failed "
        f"({total['claimed']} claimed)."
    )


@app.command("stats")
def stats() -> None:
    """Show outbox backlog: pending / published / poison (attempts exhausted), and — with the
    NATS publisher — each durable consumer's lag and dead letters."""
    from examlops.data import init_db
    from examlops.data.events import outbox_oldest_pending_age, outbox_stats

    init_db()
    s: dict[str, Any] = dict(outbox_stats())
    age = outbox_oldest_pending_age()
    s["oldest_pending_age_seconds"] = None if age is None else round(age, 1)
    backbone: dict[str, Any] | None = None
    if os.getenv("EXAMLOPS_EVENT_PUBLISHER", "log").strip().lower() == "nats":
        try:
            from examlops.events import nats_backend

            backbone = nats_backend.shared().backbone_stats()
        except Exception as exc:  # noqa: BLE001 - the outbox half is still worth showing
            backbone = {"error": str(exc)}
    if _output.json_mode:
        _output.print_json({**s, "backbone": backbone} if backbone is not None else s)
        return
    _output.print_table(
        "Event outbox",
        ["Metric", "Count"],
        [
            ["pending", str(s["pending"])],
            ["published", str(s["published"])],
            ["poison", str(s["poison"])],
            ["oldest pending (s)", "—" if age is None else f"{age:.0f}"],
        ],
    )
    if backbone is None:
        return
    if "error" in backbone:
        _output.warning(f"Backbone unreachable: {backbone['error']}")
        return
    dlq = backbone.get("dlq", {})
    names = sorted(set(backbone.get("consumers", {})) | set(dlq))
    _output.print_table(
        "Consumers",
        ["Consumer", "Pending", "Unacked", "Dead letters"],
        [
            [
                n,
                str(backbone["consumers"].get(n, {}).get("pending", "—")),
                str(backbone["consumers"].get(n, {}).get("ack_pending", "—")),
                str(dlq.get(n, 0)),
            ]
            for n in names
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


_EXAMPLES_TAIL = (
    "Examples:\n\n"
    "  # The last 20 events on the backbone\n"
    "  exa events tail\n\n"
    "  # Only retrain outcomes\n"
    "  exa events tail --topic 'retrain.run_*'\n\n"
    "  # What a consumer parked after its delivery limit\n"
    "  exa events tail --dlq autopilot"
)


@app.command("tail", epilog=_EXAMPLES_TAIL)
def tail(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=500, help="How many events"),
    topic: str | None = typer.Option(
        None, "--topic", "-t", help="Topic filter; '*' matches one segment (e.g. 'retrain.*')"
    ),
    dlq: str | None = typer.Option(
        None, "--dlq", help="Show a consumer's dead-letter subject instead of events"
    ),
) -> None:
    """Show the most recent CloudEvents on the NATS backbone (needs EXAMLOPS_NATS_URL, ADR 0124)."""
    from examlops.events import envelope, nats_backend

    try:
        js = nats_backend.shared()
        if dlq:
            subject = f"{nats_backend.DLQ_PREFIX}.{dlq}"
        elif topic:
            subject = nats_backend.subject_for(topic.replace("*", "STAR")).replace("STAR", "*")
        else:
            subject = None
        messages = js.recent(subject, limit=limit)
    except Exception as exc:  # noqa: BLE001 - surface the configuration problem, not a trace
        _output.error(
            f"Cannot read the event backbone: {exc}",
            hint="Set EXAMLOPS_NATS_URL and install 'examlops[events]'; start NATS with "
            "`docker compose --profile events up -d nats`.",
        )
        raise typer.Exit(1) from exc
    rows: list[dict] = []
    for msg in messages:
        try:
            body = json.loads(msg.data)
        except ValueError:
            body = {"raw": msg.data.decode("utf-8", "replace")}
        if dlq:
            event = body.get("event") or {}
            rows.append(
                {
                    "id": event.get("id", "?"),
                    "type": envelope.topic_of(event) if event else "?",
                    "error": body.get("error"),
                }
            )
        else:
            rows.append(
                {
                    "id": body.get("id"),
                    "type": envelope.topic_of(body),
                    "time": body.get("time"),
                    "tenant": body.get("examlopstenant"),
                    "actor": body.get("examlopsactor"),
                }
            )
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No events")
        return
    columns = list(rows[0].keys())
    title = f"Dead letters: {dlq}" if dlq else "Recent events"
    _output.print_table(title, columns, [[str(r.get(c, "")) for c in columns] for r in rows])
