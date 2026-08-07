# firewall-fix — self-healing Docker egress (lxp-cpu01 stopgap)

Keeps ExaMLOps containers able to reach the network (internal GitLab, MinIO,
external APIs) on `lxp-cpu01`, where a firewalld quirk otherwise drops all
Docker-bridge egress.

## The problem it solves

firewalld leaves a stray nftables chain `ip filter forward` with `policy drop`.
Docker's FORWARD-accept rules run first, but this chain runs second and drops
new outbound packets from the Docker bridges → containers lose DNS + TCP egress.
Two things keep re-triggering it:

- **firewalld reload** regenerates the drop chain (wiping any manual rule).
- **`docker compose down/up`** renames the bridge interface, so any fix that
  named a specific `br-…` interface stops matching. (This is exactly what broke
  ModelZoo sync: a CI deploy on 2026-06-12 recreated `examlops_default`, renaming
  its bridge from `br-4b857a5dd836` to `br-c96192629a87`.)

## How it fixes it

A tiny privileged, host-network sidecar (`alpine` + `nft`) runs a loop that
idempotently ensures this rule exists, re-adding it whenever the drop chain wipes
it:

```
nft insert rule ip filter forward ip saddr 172.16.0.0/12 accept comment "examlops-egress-fix"
```

Matching the whole Docker IPAM range (`172.16.0.0/12`) — not a bridge name —
means it survives network recreation. It needs **Docker access only, no host
sudo** (your user is in the `docker` group).

## Usage

```bash
make firewall-fix-up       # start it (run once; persists across restarts)
make firewall-fix-logs     # watch it (re-)apply the rule
make firewall-fix-down     # stop it

# or without make, from this directory:
docker compose up -d
docker compose logs -f
docker compose down
```

Verify egress is restored:

```bash
docker exec examlops-control-plane python3 -c \
  "import socket; socket.create_connection(('134.94.199.214',443),timeout=6); print('GitLab REACHABLE')"
exa modelzoo sync          # should report the latest commit, not an error
```

## One-shot recovery (no sidecar)

To re-apply the rule once, right now, without starting the watcher:

```bash
docker run --rm --privileged --network=host alpine sh -c \
  'apk add --no-cache nftables && nft insert rule ip filter forward ip saddr 172.16.0.0/12 accept comment "examlops-egress-fix"'
```

## This is a stopgap

The durable fix is a host-level **systemd one-shot** (ordered after
`firewalld.service` and `docker.service`) owned by the cluster sysadmin. The
request, with commands and verification, is drafted at
`.claude/plans/sysadmin-email-lxp-docker-egress.txt`. Keep this sidecar running
until that unit is installed; once it is, `make firewall-fix-down` and remove it.

> ⚠️ Privileged + host-network container. Review before deploying. If you do not
> want this in the public mirror, exclude it by adding `firewall-fix/` to `.dualgit/private.deny`.
