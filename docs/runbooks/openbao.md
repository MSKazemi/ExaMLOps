# Runbook: OpenBao (secrets manager)

How to initialise, unseal, use and rotate the optional OpenBao server (ADR 0011). It runs only when
you start the `secrets` Compose profile or set `secrets.openbao.enabled` in the Helm chart. It is
never published on a host port: every command below runs inside the container.

The examples use Compose from `platform/infra/docker-compose/`:

```bash
export DC="docker compose -f docker-compose.yml -f docker-compose.secrets.yml --profile secrets"
$DC up -d openbao
$DC exec openbao bao status      # exit 0 unsealed, 2 sealed or uninitialised, 1 not answering
```

On Kubernetes, replace `$DC exec openbao` with
`kubectl -n <ns> exec -it <release>-examlops-openbao-0 --`.

A server that reports **sealed** or **uninitialised** is healthy as far as the container is
concerned; the healthcheck only fails when the server does not answer. While it is sealed, the
platform keeps resolving secrets from the local store and the environment unless
`EXAMLOPS_VAULT_STRICT=1`, in which case reads fail.

## 1. Initialise (once)

```bash
$DC exec openbao bao operator init -key-shares=5 -key-threshold=3
```

This prints five unseal keys and an initial root token **once**. Copy them to your password
manager or split them between people at that moment. They are not stored anywhere else, and losing
the keys loses the data. Never paste them into `.env`, a ticket or the repository. For a throwaway
dev instance only, `-key-shares=1 -key-threshold=1` is enough.

## 2. Unseal (after every start or restart)

```bash
$DC exec openbao bao operator unseal      # run it three times, one key each time, at the prompt
```

Passing the key as an argument puts it in shell history, so use the prompt.

## 3. Enable the secrets engine the client reads

The client reads KV v2 at the mount `secret/` (path `secret/data/<path>`, field `value`). Commands
that change the server need a token; `bao login` prompts for it and keeps it in the container.

```bash
$DC exec openbao bao login                        # paste the root token at the prompt
$DC exec openbao bao secrets enable -path=secret kv-v2
```

## 4. Issue a scoped token for the platform (do not use the root token)

Write the policy from stdin, then create a periodic token bound to it:

```bash
$DC exec -T openbao bao policy write examlops - <<'HCL'
path "secret/data/*"     { capabilities = ["create", "read", "update"] }
path "secret/metadata/*" { capabilities = ["list", "read"] }
HCL
$DC exec openbao bao token create -policy=examlops -period=768h -orphan
```

Put the printed `token` in `.env` as `EXAMLOPS_VAULT_TOKEN` (Compose), or in the Secret named by
`secrets.openbao.tokenSecret` (Helm: `kubectl create secret generic examlops-openbao-token
--from-literal=token=<token>`, key `token`), then start the rest of the stack with `$DC up -d`.
Revoke the root token once you have a break-glass procedure (`bao token revoke -self`). A periodic
token stays valid as long as it is renewed within its period, so replace it before 768 hours pass
(see rotation below).

## 5. Verify

```bash
exa secrets set demo/check "any value"     # written to the local store
exa secrets get demo/check                 # prints  backend: local
$DC exec openbao bao kv put secret/demo/check value=from-vault
exa secrets get demo/check                 # now prints  backend: vault
```

`backend: vault` means OpenBao served it. If it says `local` or `env` with a warning about the
vault being unreachable, the address, the seal state or the token is wrong; the `secret_access`
audit event records the same. Once it works, set `EXAMLOPS_VAULT_STRICT=1` so an outage fails the
read instead of silently downgrading.

## Rotation

* **A platform secret:** write the new value (`bao kv put secret/<path> value=...`), then restart
  the consumers that cached it. `exa secrets rotate <path>` rotates the local-store copy.
* **The platform token:** create a new one as in step 4, update `.env` or the Secret, restart
  `control-plane`, `dashboard`, `agent` and `dataplane`, then `bao token revoke <old token>`.
* **The unseal keys:** `bao operator rekey -init -key-shares=5 -key-threshold=3`, then supply the
  current keys until it completes; distribute the new keys and destroy the old ones.
* **The root token:** generate one only when needed with `bao operator generate-root`, and revoke
  it afterwards.

## Backup and recovery

The whole state is the `openbao_data` volume (the PersistentVolume on Kubernetes). Back it up with
the server stopped. Restoring needs the unseal keys as well; a backup without them is unusable.

## Not covered

TLS on the listener, high availability (this is one node on file storage), auto-unseal, and audit
devices. Enable an audit device (`bao audit enable file file_path=/openbao/file/audit.log`) before
relying on OpenBao's own access log.
