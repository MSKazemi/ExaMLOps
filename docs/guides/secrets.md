# Secrets management & rotation

ExaMLOps resolves every platform credential through one **secrets client** instead of
plaintext `.env` files, with rotation, per-tenant scoping, an audit trail, and a CI
leak-scanner gate.

Design: ADR 0011 · spec `design/vision/specs/D7-secrets-management.md`.

## Backends (tried in order)

1. **OpenBao/Vault** — when `EXAMLOPS_VAULT_ADDR` is set (HTTP KV, best-effort).
2. **Local encrypted store** — Fernet-encrypted values in `platform_db.secrets_store`.
   The fallback works with **no running Vault**. Needs an encryption key in
   `EXAMLOPS_SECRETS_KEY` (a Fernet key) — reuses `DASHBOARD_SECRET_KEY` if unset.
3. **Environment variable** — last resort for bootstrap credentials.

A missing or access-denied secret **fails fast** with a clear, non-leaking error — a
service never starts with an empty credential.

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
```

## Tenant scoping

Paths prefixed with a known tenant (`EXAMLOPS_SECRET_TENANTS=acme,globex`) are readable
only by that tenant; the `admin` tenant is unrestricted. A denied read is recorded to
the audit trail.

## CI leak gate

The `sanity:secret-scan` GitLab job runs `exa secrets scan` over `platform/` and
`pipelines/` on every pipeline and **fails the build on any finding** — a
dependency-free alternative to gitleaks/trufflehog. It skips vendored/generated dirs
(`node_modules`, `.venv`, lockfiles, …).

## `.env` exposure remediation runbook (order matters)

Follow this order so the deploy never breaks (spec R6/R8/R9):

1. **Rotate** every currently-exposed secret (`exa secrets rotate …`).
2. **Switch deploy** to manager-injected secrets and verify services start.
3. **Untrack** `.env` (`git rm --cached .env`; ensure it's in `.gitignore`).
4. **Scrub** the secrets from git history (`git filter-repo` / BFG).
5. Confirm the **`sanity:secret-scan`** gate is green so no new secret can land.
