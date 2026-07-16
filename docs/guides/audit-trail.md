# Immutable, Tamper-Evident Audit Trail (D4)

> Next-Gen 40 · feature **D4** · ADR 0028 · spec `design/vision/specs/D4-immutable-audit-trail.md`

D4 upgrades the existing `audit_events` log into an **append-only, hash-chained,
periodically-signed** trail with an integrity verifier. Every governance action
(approvals, promotions, drift auto-retrains, guardrail blocks, RBAC changes, compliance
gates, …) already flows through `write_audit_event` — D4 makes that stream tamper-evident
with **no change to the ~170 call sites**.

## How the chain works

Each event stores:

- `prev_hash` — the hash of the previous event (or `GENESIS` for the first),
- `hash = SHA256(prev_hash ‖ canonical(event))` over a deterministic serialization of the
  event fields (source, actor, action, target, details, tenant, ts).

Any edit, deletion, or reordering changes a hash and breaks the chain from that point on —
which `exa audit verify` detects and localizes.

## Append-only enforcement

DB-level triggers block `UPDATE` and `DELETE` on `audit_events`:

```sql
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only (D4)'); END;
```

So even direct SQL cannot silently rewrite history through the normal connection.

## Verifying integrity

```bash
exa audit verify
# ✓ Audit chain verified — 1423 chained event(s) intact (head a1b2c3d4e5f6…).
```

If the trail was tampered with:

```bash
exa audit verify
# ✗ AUDIT CHAIN BROKEN at event id 337: hash mismatch (event altered).
```

`exa audit verify` exits non-zero on a broken chain, so it can gate CI or a compliance
check.

## Signed checkpoints

The chain head can be signed periodically, producing a detached signature that proves the
trail's state at that point (async/batched — not per event):

```bash
exa audit checkpoint          # sign the current head (D3 HMAC key / EXAMLOPS_SIGNING_KEY)
exa audit checkpoints         # list signed checkpoints
```

## Archival export & retention

Export is **read-only** — the trail is retained in place (append-only), and the export
action is itself audited:

```bash
exa audit export --out audit_2026.json
exa audit export --out old.json --before 2026-01-01T00:00:00
```

## The log view (unchanged)

The familiar log view still works exactly as before:

```bash
exa audit --last 7d
exa audit --model JPCP --action promotion
exa --json audit --last 30d
```

## Coverage

Every governed action carries **actor + tenant (D6) + resource** and is chained. Sources
across the platform (CLI, agent, bridge, control plane) all write through the same
`write_audit_event`, so the chain is complete by construction. PII in details should be
stored by reference / redacted (see D8).

## Programmatic use

```python
from examlops import platform_db

platform_db.write_audit_event("cli", "alice", "promotion", "JPCP", tenant="acme")
result = platform_db.verify_audit_chain()      # {'ok': True, 'count': N, 'head_hash': ...}
head = platform_db.audit_chain_head()
platform_db.sign_audit_checkpoint(signature, key_id="d3-hmac")
events = platform_db.export_audit_events()     # read-only
```

## See also

- [NIST AI RMF (D2)](governance-nist-rmf.md) — the `record_keeping` evidence this feeds.
- [EU AI Act (D1)](eu-ai-act-compliance.md) — Art. 12 record-keeping conformance.
- [Supply-chain security (D3)](supply-chain-security.md) — the signing key reused for checkpoints.
