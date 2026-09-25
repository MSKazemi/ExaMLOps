# Secrets management & rotation

ExaMLOps resolves every platform credential through one **secrets client** instead of
plaintext `.env` files, with rotation, per-tenant scoping, an audit trail, and a CI
leak-scanner gate.

Design: ADR 0011 · spec `design/vision/specs/D7-secrets-management.md`.

## Backends (tried in order)

1. **OpenBao/Vault** — when `EXAMLOPS_VAULT_ADDR` is set (HTTP KV v2; mount
   `EXAMLOPS_VAULT_MOUNT`, default `secret`; optional `EXAMLOPS_VAULT_NAMESPACE`; timeout
   `EXAMLOPS_VAULT_TIMEOUT`, default 5 s).
2. **SOPS + age** — when `EXAMLOPS_SOPS_FILE` names a SOPS-encrypted YAML/JSON document. The
   file holds only ciphertext and is safe to commit; `sops` decrypts **one key per read**
   (`sops decrypt --extract`) with the age identity in `SOPS_AGE_KEY_FILE`/`SOPS_AGE_KEY`.
   The path `control-plane/token` is the key `{"control-plane": {"token": …}}`. The binary is
   found on `PATH` or named by `EXAMLOPS_SOPS_BIN`; every call is bounded by
   `EXAMLOPS_SOPS_TIMEOUT` (default 10 s). This is the dev/CI fallback ADR 0011 chose.
3. **Local encrypted store** — Fernet-encrypted values in `platform_db.secrets_store`.
   Works with **no running Vault and no sops binary**. Needs an encryption key in
   `EXAMLOPS_SECRETS_KEY` (a Fernet key) — reuses `DASHBOARD_SECRET_KEY` if unset.
4. **Environment variable** — last resort for bootstrap credentials.

A SOPS failure (no binary, missing file, wrong age identity, timeout) is reported exactly like a
Vault outage — `sops_error` on the `secret_access` audit event and a warning — and
`EXAMLOPS_VAULT_STRICT=1` refuses the fallback for both managers. A key that the document simply
does not hold falls through quietly.

### Where writes go

`exa secrets set` and `exa secrets rotate` write to **one** store, chosen by
`EXAMLOPS_SECRETS_WRITE_BACKEND`:

| Value | Effect |
|---|---|
| `local` (default) | Fernet-encrypted row in `platform.db` (versioned) |
| `vault` | a new KV v2 version in OpenBao/Vault — a rotation lands in the manager of record |
| `sops` | `sops set --value-stdin` re-encrypts the document in place (unversioned, reported as v0); the value never appears on a command line |

A manager that cannot be reached **fails the write** (`secret_set_failed` audit event); nothing
is written to another store instead. Vault and SOPS hold one namespace shared by every tenant, so
a write there passes the same tenant path-prefix check as a read. `exa secrets backends` shows the
state of every tier and where writes go.

```bash
$ exa secrets backends
 Backend  State
 vault    reachable, unsealed
 sops     not configured
 local    keys primary (active primary)
 env      last resort
Writes go to: vault · strict (no fallback on a manager outage)
```

A missing or access-denied secret **fails fast** with a clear, non-leaking error — a
service never starts with an empty credential.

### Knowing which backend served a value

The fallback chain is a feature — a Vault blip must not take the platform down — but it
moves a credential between trust domains, so it is never silent:

* `exa secrets get <path>` prints the **backend** that served the value (`vault`, `local`
  or `env`), and warns when a configured Vault could not be reached.
* Every read writes a `secret_access` audit event carrying `backend`, plus `vault_error`
  when the Vault was configured but did not answer. A clean Vault read and a downgrade to
  an environment variable are therefore distinguishable in the audit trail.
* A Vault **404** is not a degradation: the Vault answered, the secret is simply not
  there, and falling through is correct. Only a genuine outage (connection refused, denied
  token, malformed reply) is reported as one.
* Set **`EXAMLOPS_VAULT_STRICT=1`** to refuse the downgrade altogether — an unreachable
  Vault then fails the read instead of serving whatever the local store or the environment
  happens to hold. Recommended wherever Vault is the system of record.

```bash
$ EXAMLOPS_VAULT_ADDR=https://vault.example:8200 exa secrets get control-plane/token
⚠ vault unreachable (URLError: ...) - this value came from the local store, which may
  hold something different. Set EXAMLOPS_VAULT_STRICT=1 to fail instead.
  backend: local
```

## Running OpenBao

OpenBao is shipped as an opt-in, and nothing uses it until you say so. It is not started by
`make stack-up`, no service is told about it by default, and it publishes no port.

**Compose** (from `platform/infra/docker-compose/`):

```bash
docker compose -f docker-compose.yml -f docker-compose.secrets.yml --profile secrets up -d openbao
# initialise and unseal it once: docs/runbooks/openbao.md
docker compose -f docker-compose.yml -f docker-compose.secrets.yml --profile secrets up -d
```

The first file defines the server (`openbao/openbao`, pinned by tag and digest, file storage on the
`openbao_data` volume, non-root, read-only, reachable only as `openbao:8200` on the internal
network). The second, `docker-compose.secrets.yml`, is the only thing that sets
`EXAMLOPS_VAULT_ADDR`/`EXAMLOPS_VAULT_TOKEN`/`EXAMLOPS_VAULT_STRICT` on `control-plane`,
`dashboard`, `agent` and `dataplane`; the token comes from `.env`.

**Helm:** `--set secrets.openbao.enabled=true` deploys a one-replica StatefulSet with a
PersistentVolumeClaim, a ClusterIP Service and a NetworkPolicy; create the token Secret named by
`secrets.openbao.tokenSecret` after initialising the server.

The client reads KV version 2 at the mount `secret/`, path `secret/data/<path>`, field `value`, so
the runbook enables that mount. Confirm which backend is in use with `exa secrets get <path>`,
which prints `backend: vault|local|env`.

Limits, stated plainly: the listener is plain HTTP (internal network only, put TLS in front before
exposing it), and a single node on file storage is not highly available.

## Startup injection (no plaintext credential in the environment)

A service's environment can name **where** a credential lives instead of holding it:

```bash
CONTROL_PLANE_TOKEN=secret://control-plane/token          # through the secrets client
AGENT_API_KEY=secret://acme/agent-key?tenant=acme         # read as a tenant
DASHBOARD_JWT_SECRET=secret+file:///run/secrets/dashboard_jwt   # a mounted secret file
```

The control plane, dashboard, agent, dataplane and LLM gateway resolve every such reference at
start-up, before they read any configuration, and replace it in their own process environment —
so every existing setting reads the real value. Each resolution is a `secret_access` audit event
by `service:<name>`, plus one `secrets_injected` summary naming the variables and the backends
that served them (never a value).

* **Fail closed.** An unresolvable reference stops the service with `SecretInjectionError`,
  naming the variable and the reason. With `EXAMLOPS_SECRETS_INJECT_STRICT=0` it starts anyway
  and the variable is *removed* — never left holding the literal `secret://…` string, which a
  token check would otherwise accept as the credential.
* **Bounded.** At most 256 references; a `secret+file://` path must be absolute, a regular file
  and at most 64 KiB (one trailing newline is cut, as Docker/Kubernetes secret files carry one).
* **Idempotent** and a no-op when the environment holds no reference.
  `EXAMLOPS_SECRETS_INJECT=0` switches it off: references are then *removed* unresolved (for the
  same reason) and a warning is logged.

`exa secrets refs` audits an environment — this process's, or a dotenv file with `--env-file` —
and classifies every credential-carrying variable as `reference`, `file`, `bootstrap` (the store's
own key or token, which cannot point at the store) or `plaintext`, and whether each reference
resolves. `--strict` exits 1 on any plaintext credential or unresolvable reference, which makes it
a deploy gate:

```bash
exa secrets refs --env-file .env --strict
```

## Dynamic short-lived credentials (leases)

When an OpenBao dynamic secrets engine is mounted (`database/creds/<role>` and the like), the
platform can mint a credential that the manager itself expires:

```bash
exa secrets lease issue database/creds/readonly          # lease id + TTL; fields redacted
exa secrets lease issue database/creds/readonly --reveal # print the fields (not from the dashboard)
exa secrets lease renew <lease-id> --increment 3600      # engine-capped
exa secrets lease revoke <lease-id>                      # end it now
```

A path that returns no lease (a static KV secret) is refused rather than presented as
short-lived. Issue, renew and revoke are audited with the lease id and TTL, never the credential.
Configuring the engine itself (database connection, roles, TTLs) is OpenBao policy, done with
`bao` as in `docs/runbooks/openbao.md`.

## CLI

```bash
# Generate an encryption key once:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export EXAMLOPS_SECRETS_KEY=<that key>

exa secrets set control-plane/token s3cr3t          # store encrypted (audited)
exa secrets get control-plane/token                 # redacted by default
exa secrets get control-plane/token --reveal        # print plaintext (dangerous)
exa secrets rotate control-plane/token              # rotate to a fresh value (audited)
exa secrets list                                    # metadata only — never values
exa secrets scan .env                               # leak scan; exit 1 on any finding
exa secrets backends                                # every tier's state + where writes go
exa secrets refs --strict                           # no plaintext credential in the env
```

## Tenant scoping

Paths prefixed with a known tenant (`EXAMLOPS_SECRET_TENANTS=acme,globex`) are readable
only by that tenant; the `admin` tenant is unrestricted. A denied read is recorded to
the audit trail.

* A path with an empty, `.` or `..` segment (or a control character) is refused outright: OpenBao
  cleans request paths, so `acme/../globex/key` or `/globex/key` would otherwise pass the prefix
  check as one tenant and be served another's secret.
* The vault and the SOPS document are **one namespace shared by every tenant**. A write there must
  pass the same prefix check as a read and, under `EXAMLOPS_MULTITENANCY`, an unprefixed (shared)
  path may be written only by `admin` — otherwise one tenant could replace a secret every tenant
  reads. The local store is keyed by tenant, so this does not apply to it.
* `lease renew` / `lease revoke` take `--tenant` and apply the same rule to the lease id (which
  starts with the engine path), so one tenant cannot extend or cut off another's credential.

## CI leak gate

The `sanity:secret-scan` GitLab job runs `exa secrets scan` over `platform/` and
`pipelines/` on every pipeline and **fails the build on any finding** — a
dependency-free alternative to gitleaks/trufflehog. It skips vendored/generated dirs
(`node_modules`, `.venv`, lockfiles, …).

## `.env` exposure remediation runbook (order matters)

Follow this order so the deploy never breaks (spec R6/R8/R9):

1. **Rotate** every currently-exposed secret (`exa secrets rotate …`).
2. **Switch deploy** to manager-injected secrets (`VAR=secret://<path>`), gate it with
   `exa secrets refs --env-file <deploy .env> --strict`, and verify services start.
3. **Untrack** `.env` (`git rm --cached .env`; ensure it's in `.gitignore`).
4. **Scrub** the secrets from git history (`git filter-repo` / BFG).
5. Confirm the **`sanity:secret-scan`** gate is green so no new secret can land.
