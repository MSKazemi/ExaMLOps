# DataPlane — Dev Setup Guide

The real DataPlane (Rust server + inference request generator) lives in the companion `dataplane` repository at `../dataplane/`. This replaces the former internal Python simulator (`dataplane_sim.py`).

## Architecture

```
┌─────────────────────────────────┐     ┌──────────────────────────────────────┐
│  dataplane repo                 │     │  ai-productions repo                 │
│                                 │     │                                      │
│  DataPlane (Rust, TCP :<PORT>)    │◄────│  dataplane_bridge.py                 │
│  examlops-reqgen (JPCP jobs)    │     │    registers JPCP/MACK/... handlers  │
│                                 │────►│    calls Ray Serve                   │
│  logs/inference_requests.log    │     │    returns predictions               │
└─────────────────────────────────┘     └──────────────────────────────────────┘
         dataplane-net (Docker network — both containers join it)
```

## Setup (once)

Create the shared Docker network that connects both compose stacks:

```bash
docker network create dataplane-net
```

## Running

**Option A — start everything at once** (from ai-productions):

```bash
make full-up   # starts reqgen + bridge automatically alongside the full stack
```

**Option B — manual, two terminals**

Terminal 1 — real DataPlane (from the `dataplane` repo root):

```bash
cd ../dataplane
docker compose up -d        # starts DataPlane + JPCP reqgen
```

Terminal 2 — bridge (from ai-productions):

```bash
make dataplane-up              # bridge connects to dataplane-reqgen:<PORT>
make dataplane-bridge-logs     # watch bridge ← REQ / → RES paired log lines
make dataplane-reqgen-logs     # watch reqgen inference_requests.log + dataplane.log
```

## Pointing the bridge at DataPlane (host configuration)

The bridge runs **inside a container**, so `localhost` means *the container*, not the host.
Set `DATAPLANE_HOST` according to where DataPlane runs:

| DataPlane runs as… | `DATAPLANE_HOST` | Requires |
|---|---|---|
| Container on `dataplane-net` (default) | `dataplane-reqgen` (container name) | both on `dataplane-net` |
| **Bare-metal process on the host** (stable) | `host.docker.internal` | `extra_hosts: host.docker.internal:host-gateway` on the bridge service (already in compose) |
| **Bare-metal process on the host** (quick) | the Docker bridge gateway IP, e.g. `172.19.0.1` | nothing extra; find it with `docker network inspect examlops_default -f '{{(index .IPAM.Config 0).Gateway}}'` |
| Bridge also runs bare-metal (`make dataplane-bridge-up`) | `localhost` | bridge not containerized |

> A **bare-metal** DataPlane must bind `0.0.0.0:<PORT>` (not `127.0.0.1`) — a loopback-only bind is
> unreachable from containers even via the gateway. Check on the host: `ss -ltnp | grep <PORT>`.

**Where to set it (precedence):** for `make stack-up`, the bridge's compose `environment:` block
overrides its `env_file:`, and `${DATAPLANE_HOST}` is interpolated from the **root** `.env` (the
Makefile passes `--env-file <root>/.env`). So set `DATAPLANE_HOST` / `DATAPLANE_PORT` in the root
`.env`, not the compose-dir copy. Confirm the resolved value:

```bash
docker compose --env-file "$(pwd)/.env" --profile dataplane config | grep DATAPLANE_
```

See the inline comments in `.env.example` for the same guidance.

## Expected Bridge Log Output

Once both are running you should see paired `← REQ` / `→ RES` lines for every JPCP request:

```
2026-05-26 23:10:06 [dataplane-bridge] INFO: Registered per-model handler | model=JPCP uuid=30b0f24c-...
2026-05-26 23:10:06 [dataplane-bridge] INFO: Registered per-model handler | model=MACK uuid=65611ddc-...
2026-05-26 23:10:06 [dataplane-bridge] INFO: Registered per-model handler | model=MCBOUND uuid=1a2c3b5d-...
2026-05-26 23:10:07 [dataplane-bridge] INFO: ← REQ  job=243631d7  model=JPCP  alias=Production  nodes=42  user=1234
2026-05-26 23:10:07 [dataplane-bridge] INFO: → RES  job=243631d7  model=JPCP  prediction=91.36W  version=18  run=5f015850  latency=163ms
```

## Expected DataPlane reqgen log output

Once the bridge connects and registers, the reqgen transitions from `[?]` to `[ok]`:

```
# Before bridge connects:
2026-05-26 23:10:05.000  [?]  job=e253ccf4  unexpected payloadType=0

# After bridge registers:
2026-05-26 23:10:07.123  [ok]  job=243631d7   163ms  power_per_node_watts=91.36W  version=18  alias=Production  run=5f015850
```

## UUID Alignment

Model UUIDs are defined in `pipelines/models/<name>.yaml` (`dataplane_uuid`) and must match the UUIDs in `../dataplane/ai-production-inference-request-generator/models.yaml`.

| Model | UUID | Status |
|---|---|---|
| JPCP | `<UUID>` | reqgen sends + bridge handles |
| MACK | `<UUID>` | bridge handles (reqgen JPCP-only for now) |
| MCBound | `<UUID>` | bridge handles (reqgen JPCP-only for now) |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `[?] unexpected payloadType=0` in reqgen | Bridge not yet registered | Wait for bridge to start and register; these stop once connected |
| `[Errno -3] Temporary failure in name resolution` in bridge (loops on reconnect) | reqgen container stopped (exit 137 = OOM or `docker kill`) | `cd ../dataplane && docker compose up -d` — or `make full-up` |
| `Connection refused` in bridge | dataplane container not running or wrong network | Start `docker compose up -d` in dataplane repo; check `dataplane-net` network exists |
| `Name or service not known` | `host.docker.internal` failed — Linux Docker | Ensure `extra_hosts: host.docker.internal:host-gateway` is in docker-compose (already set) |
| No `[ok]` lines in reqgen but bridge shows RES | UUID mismatch | Compare `dataplane_uuid` in `pipelines/models/jpcp.yaml` with `models.yaml` in dataplane repo |
