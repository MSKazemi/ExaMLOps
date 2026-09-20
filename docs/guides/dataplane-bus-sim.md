# Dataplane bus — Dev Setup Guide

The real Dataplane bus (Rust server + inference request generator) lives in the companion `dataplane-bus` repository at `../dataplane-bus/`. This replaces the former internal Python simulator (`dataplane_bus_sim.py`).

## Architecture

```
┌─────────────────────────────────┐     ┌──────────────────────────────────────┐
│  dataplane-bus repo                 │     │  ai-productions repo                 │
│                                 │     │                                      │
│  Dataplane bus (Rust, TCP :5398)    │◄────│  dataplane_bus_bridge.py                 │
│  examlops-reqgen (JPCP jobs)    │     │    registers JPCP/MACK/... handlers  │
│                                 │────►│    calls Ray Serve                   │
│  logs/inference_requests.log    │     │    returns predictions               │
└─────────────────────────────────┘     └──────────────────────────────────────┘
         dataplane-bus-net (Docker network — both containers join it)
```

## Setup (once)

Create the shared Docker network that connects both compose stacks:

```bash
docker network create dataplane-bus-net
```

## Running

**Option A — start everything at once** (from ai-productions):

```bash
make full-up   # starts reqgen + bridge automatically alongside the full stack
```

**Option B — manual, two terminals**

Terminal 1 — real Dataplane bus (from the `dataplane-bus` repo root):

```bash
cd ../dataplane-bus
docker compose up -d        # starts Dataplane bus + JPCP reqgen
```

Terminal 2 — bridge (from ai-productions):

```bash
make dataplane-bus-up              # bridge connects to dataplane-bus-reqgen:5398
make dataplane-bus-bridge-logs     # watch bridge ← REQ / → RES paired log lines
make dataplane-bus-reqgen-logs     # watch reqgen inference_requests.log + dataplane-bus.log
```

## Pointing the bridge at Dataplane bus (host configuration)

The bridge runs **inside a container**, so `localhost` means *the container*, not the host.
Set `DATAPLANE_BUS_HOST` according to where Dataplane bus runs:

| Dataplane bus runs as… | `DATAPLANE_BUS_HOST` | Requires |
|---|---|---|
| Container on `dataplane-bus-net` (default) | `dataplane-bus-reqgen` (container name) | both on `dataplane-bus-net` |
| **Bare-metal process on the host** (stable) | `host.docker.internal` | `extra_hosts: host.docker.internal:host-gateway` on the bridge service (already in compose) |
| **Bare-metal process on the host** (quick) | the Docker bridge gateway IP, e.g. `172.19.0.1` | nothing extra; find it with `docker network inspect examlops_default -f '{{(index .IPAM.Config 0).Gateway}}'` |
| Bridge also runs bare-metal (`make dataplane-bus-bridge-up`) | `localhost` | bridge not containerized |

> A **bare-metal** Dataplane bus must bind `0.0.0.0:5398` (not `127.0.0.1`) — a loopback-only bind is
> unreachable from containers even via the gateway. Check on the host: `ss -ltnp | grep 5398`.

**Where to set it (precedence):** for `make stack-up`, the bridge's compose `environment:` block
overrides its `env_file:`, and `${DATAPLANE_BUS_HOST}` is interpolated from the **root** `.env` (the
Makefile passes `--env-file <root>/.env`). So set `DATAPLANE_BUS_HOST` / `DATAPLANE_BUS_PORT` in the root
`.env`, not the compose-dir copy. Confirm the resolved value:

```bash
docker compose --env-file "$(pwd)/.env" --profile dataplane-bus config | grep DATAPLANE_BUS_
```

See the inline comments in `.env.example` for the same guidance.

## Expected Bridge Log Output

Once both are running you should see paired `← REQ` / `→ RES` lines for every JPCP request:

```
2026-05-26 23:10:06 [dataplane-bus-bridge] INFO: Registered per-model handler | model=JPCP uuid=30b0f24c-...
2026-05-26 23:10:06 [dataplane-bus-bridge] INFO: Registered per-model handler | model=MACK uuid=65611ddc-...
2026-05-26 23:10:06 [dataplane-bus-bridge] INFO: Registered per-model handler | model=MCBOUND uuid=1a2c3b5d-...
2026-05-26 23:10:07 [dataplane-bus-bridge] INFO: ← REQ  job=243631d7  model=JPCP  alias=Production  nodes=42  user=1234
2026-05-26 23:10:07 [dataplane-bus-bridge] INFO: → RES  job=243631d7  model=JPCP  prediction=91.36W  version=18  run=5f015850  latency=163ms
```

## Expected Dataplane bus reqgen log output

Once the bridge connects and registers, the reqgen transitions from `[?]` to `[ok]`:

```
# Before bridge connects:
2026-05-26 23:10:05.000  [?]  job=e253ccf4  unexpected payloadType=0

# After bridge registers:
2026-05-26 23:10:07.123  [ok]  job=243631d7   163ms  power_per_node_watts=91.36W  version=18  alias=Production  run=5f015850
```

## UUID Alignment

Model UUIDs are defined in `pipelines/models/<name>.yaml` (`dataplane_bus_uuid`) and must match the UUIDs in `../dataplane-bus/ai-production-inference-request-generator/models.yaml`.

| Model | UUID | Status |
|---|---|---|
| JPCP | `30b0f24c-e154-432b-91f9-25a144095a30` | reqgen sends + bridge handles |
| MACK | `65611ddc-2a9c-4b88-bcc7-2f11684eb649` | bridge handles (reqgen JPCP-only for now) |
| MCBound | `1a2c3b5d-6873-41cb-b221-1612a8787e2c` | bridge handles (reqgen JPCP-only for now) |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `[?] unexpected payloadType=0` in reqgen | Bridge not yet registered | Wait for bridge to start and register; these stop once connected |
| `[Errno -3] Temporary failure in name resolution` in bridge (loops on reconnect) | reqgen container stopped (exit 137 = OOM or `docker kill`) | `cd ../dataplane-bus && docker compose up -d` — or `make full-up` |
| `Connection refused` in bridge | dataplane-bus container not running or wrong network | Start `docker compose up -d` in dataplane-bus repo; check `dataplane-bus-net` network exists |
| `Name or service not known` | `host.docker.internal` failed — Linux Docker | Ensure `extra_hosts: host.docker.internal:host-gateway` is in docker-compose (already set) |
| No `[ok]` lines in reqgen but bridge shows RES | UUID mismatch | Compare `dataplane_bus_uuid` in `usecases/reference/models/jpcp.yaml` with `models.yaml` in dataplane-bus repo |
