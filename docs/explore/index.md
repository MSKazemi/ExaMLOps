---
title: System map
description: The whole ExaMLOps platform on one interactive map — every service, and the five lines that connect them.
hide:
  - navigation
  - toc
---

# System map

ExaMLOps is easiest to understand as a transit map. Five kinds of work travel through the
platform, and each has its own line. The lines share stations: Ray Serve is a stop on the data
line and on the control line, and the control plane is where the control line and the decision
line meet.

<div class="xm-player" data-scene="system" data-mode="ambient"></div>

Open any station for what it does, the commands that operate it and the guide that explains it.
Pick a line in the legend to follow it on its own.

## The five lines

<div class="xm-lines" markdown>
<div class="xm-line-item" style="--sw: var(--xm-data)" markdown>
<span class="xm-line-swatch"></span>

<h3>Data line</h3>

A prediction: from a client, across the bus, through batching and routing to a model on Ray Serve, and back.

[Follow a prediction](prediction.md)

</div>
<div class="xm-line-item" style="--sw: var(--xm-control)" markdown>
<span class="xm-line-swatch"></span>

<h3>Control line</h3>

A retrain: from a trigger to the control plane, through Prefect and a scheduler, into the registry and back into serving.

[Follow a retrain](retrain.md)

</div>
<div class="xm-line-item" style="--sw: var(--xm-human)" markdown>
<span class="xm-line-swatch"></span>

<h3>Decision line</h3>

A person decides: approvals, promotions, traffic splits, the autopilot switch and cluster admission.

[See who decides](decisions.md)

</div>
<div class="xm-line-item" style="--sw: var(--xm-hpc)" markdown>
<span class="xm-line-swatch"></span>

<h3>Compute line</h3>

A cluster job: discover a cluster, approve it, place the job, run it and account for its GPU-hours.

[Follow a cluster job](hpc.md)

</div>
<div class="xm-line-item" style="--sw: var(--xm-observe)" markdown>
<span class="xm-line-swatch"></span>

<h3>Signal line</h3>

Telemetry and evidence: metrics, logs, traces, alerts and the hash-chained audit trail.

[Follow the signals](signals.md)

</div>
<div class="xm-line-item is-planned" style="--sw: var(--xm-planned)" markdown>
<span class="xm-line-swatch"></span>

<h3>Under construction</h3>

Dotted track marks work that is designed but not built. Every dotted segment is listed on the roadmap.

[Read the roadmap](roadmap.md)

</div>
</div>

## Where to go next

| You want to… | Read |
|---|---|
| Know what each service does and which port it listens on | [Services and their jobs](services.md) |
| Find the command for a task | [Every capability](capabilities.md) — all `exa` commands, searchable |
| Know what is built and what is next | [Roadmap](roadmap.md) |
| Run it yourself | [Quick Start](../guides/quickstart.md) |
