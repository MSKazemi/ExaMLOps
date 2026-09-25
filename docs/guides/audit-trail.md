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

It also reports what it could **not** check:

```bash
exa audit verify
# ✓ Audit chain verified — 6779 chained event(s) intact (head a1b2c3d4e5f6…).
# ⚠ 1408 event(s) carry no hash and were not verified — they are outside the chain, so their
#   order and presence are not tamper-evident…
```

### `ok` and `fully_verified` answer different questions

A compliance script asks the second one, and until 2026-09-14 only the first existed:

| Field | Means | A log with pre-chain history |
|---|---|---|
| `ok` | the chain that exists is intact | **true** |
| `fully_verified` | **every** row was checked | **false** |

`ok` has to stay true over unchained rows — crying wolf about pre-chain history that cannot be
retro-fitted (rewriting the log is the one thing an append-only trail must never do) would make the
command useless. So `ok` cannot be the field that answers *is my audit trail sound*. `fully_verified`
is. The exit code still follows `ok`, deliberately and unchanged.

```bash
exa --json audit verify | jq -e '.fully_verified' \
  || echo "part of the log could not be checked — see .unchained and .chain_begins_at"
```

**The warning is printed in `--json` mode too**, on stderr — stdout still carries exactly one
document, and a scripted compliance run no longer passes in silence over a log it only partly read.

A row with a NULL `hash` is outside the chain and cannot be recomputed, so it is counted
separately rather than skipped. `ok` stays true — the chain that exists is intact — but the claim
is narrowed out loud, because a verifier that silently ignores what it cannot check reports
success over a log it has only partly read. Unchained rows are still protected from SQL-level
edits by the append-only triggers; what they lack is proof of **order and presence**.

**Two causes, and they need telling apart.** `verify` reports `chain_begins_at`, the timestamp of
the oldest chained event:

- **Before that point** — events written before the chain columns were added. Expected, benign, and
  deliberately *not* retro-fitted: back-filling hashes would mean rewriting the log, which is the
  one thing an append-only audit trail must never do. They age out with retention.
- **After it** — a writer is still bypassing `write_audit_event`. That is a bug; find it.

Until 2026-09-02 the dashboard was such a writer: sixteen routers and five modules used raw
`INSERT`s.

## Signed checkpoints

The chain head can be signed periodically, producing a detached signature that proves the
trail's state at that point (async/batched — not per event):

```bash
exa audit checkpoint          # sign the current head (D3 HMAC key / EXAMLOPS_SIGNING_KEY)
exa audit checkpoints         # list signed checkpoints
exa audit checkpoint --anchor --skip-unchanged   # cron form: exit 1 unless durably anchored
```

### Anchoring off the platform (WORM)

A checkpoint is also appended to a hash-chained WORM log outside the database, so a full-database
rewrite cannot re-chain a forged history unnoticed. Set `EXAMLOPS_AUDIT_WORM_PATH` to either

- a **file** (local append-only chain; development), or
- **`s3://bucket/prefix`** — one S3 **Object-Lock** object per entry (`<prefix>/000000000001.json`,
  ...), written with `ObjectLockMode` (`EXAMLOPS_AUDIT_WORM_S3_MODE`, default `GOVERNANCE`; use
  `COMPLIANCE` in production) and `ObjectLockRetainUntilDate`
  (`EXAMLOPS_AUDIT_WORM_S3_RETAIN_DAYS`) and `If-None-Match: *`, so an existing entry is never
  overwritten. The bucket must be created with Object Lock enabled. It needs boto3
  (`pip install 'examlops[backup]'`).

If the S3 write fails, the failure is **counted** (`audit_worm.anchor_failures()`), logged at ERROR,
and the entry degrades to `EXAMLOPS_AUDIT_WORM_FALLBACK_PATH`; the command reports the checkpoint as
*not* durably anchored (and `--anchor` exits 1). `exa audit verify-worm` reads the S3 chain, checks
every DB checkpoint is anchored, and warns about checkpoints that exist only in the fallback file.

### Transparency log (Rekor / Sigstore)

The WORM store proves the platform's copy was not rewritten — unless whoever controls that bucket
is the attacker. A public **transparency log** removes that last trust: once a checkpoint is in
Rekor, anyone with the log's public key can prove the chain head existed at the log's integration
time, and nobody can remove or back-date it. Choose a backend with `EXAMLOPS_AUDIT_TRANSPARENCY`:

| Backend | Configure | What is logged |
|---|---|---|
| `rekor` (implied by `EXAMLOPS_AUDIT_REKOR_URL` alone) | `EXAMLOPS_AUDIT_REKOR_URL`, a dedicated **ECDSA P-256** key in `EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE` (or the secret `audit/transparency-ecdsa-private`) | a `hashedrekord` entry over the statement `examlops-audit-checkpoint/v1\n<head_id>\n<head_hash>\n` |
| `sigstore` | `pip install 'examlops[audit-sigstore]'` and an OIDC identity (ambient in CI, or `EXAMLOPS_AUDIT_SIGSTORE_TOKEN`) | a keyless Fulcio-certificate signature whose bundle carries the Rekor inclusion |

Every new checkpoint (`exa audit checkpoint` and the scheduled job below) is logged; the receipt
is kept in `audit_transparency_entries`, one per checkpoint, so a re-run never logs twice. The
signature is deterministic (RFC 6979), so a retry after a lost response meets Rekor's `409` and
follows its `Location` instead of creating a second entry. Security defaults: a non-HTTPS URL is
refused unless it is the loopback interface, responses are capped at 1 MiB, every request has a
timeout (`EXAMLOPS_AUDIT_REKOR_TIMEOUT`, default 10 s). A failed upload never loses the
checkpoint: it is reported as `transparency_error`, counted, and retried by the next run.

```bash
export EXAMLOPS_AUDIT_REKOR_URL=https://rekor.sigstore.dev
openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt > tlog.pem
export EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE=$PWD/tlog.pem
exa audit checkpoint                  # signs, anchors to WORM, logs to Rekor
exa audit verify-transparency         # re-reads each receipt from the log; exit 1 on a mismatch
```

`exa audit verify-transparency` re-fetches every recorded entry and checks that the logged head
is still the audit chain's event at that id (a head pruned under an audited retention cut is
exempt; a rewritten or missing one fails), that the log's copy records this checkpoint's digest,
that it was signed by the **platform's** key — `EXAMLOPS_AUDIT_TRANSPARENCY_PUBLIC_KEY_FILE` (a
verifier holding only the public half) or the public half of the signing key, never the key
inside the log entry, which anyone could have uploaded — that the log index matches the receipt
and, with `EXAMLOPS_AUDIT_REKOR_PUBLIC_KEY_FILE` set to the log's public key, the log's Signed
Entry Timestamp. With no trusted key it fails closed. It warns about signed checkpoints newer than
the last logged head, and says so when a `sigstore` bundle was checked for structure only (digest
and tlog entry; the Fulcio certificate chain and signer identity are not verified yet).

### The schedule

The periodic half of ADR 0028 is a job, not a reminder. The **control plane** runs it in the
background every `EXAMLOPS_AUDIT_MAINTENANCE_SECONDS` (default 3600; `0` disables); on a host
without a control plane run `exa audit maintain` (a loop) or `exa audit maintain --once` from cron.
One cycle:

1. signs and anchors the current head (`checkpoint_and_anchor(skip_if_unchanged=True)` — a head
   that is already signed, anchored and logged costs one read);
2. logs it to the transparency log, when one is configured;
3. prunes under the retention policy — **only** with `EXAMLOPS_AUDIT_PRUNE_SCHEDULED=1` and
   `EXAMLOPS_AUDIT_RETENTION_DAYS` set, archiving to `EXAMLOPS_AUDIT_ARCHIVE_DIR` (default
   `<EXAMLOPS_DATA_DIR>/audit-archive`) and keeping every gate the manual prune has.

A cycle holds the cluster-wide coordinator lease `audit-maintenance`, so replicas and a cron job
never overlap (`EXAMLOPS_AUDIT_MAINTENANCE_LEASE_TTL`). A missing signing key, a degraded WORM
write, a failed upload or a refused prune makes the cycle `degraded`: it is recorded, counted in
`examlops_audit_maintenance_errors_total` (alert `AuditMaintenanceFailing`), and the next step
still runs. `examlops_audit_maintenance_last_success_timestamp_seconds` is the heartbeat.

```bash
exa audit maintain --dry-run          # what the next cycle would do; changes nothing
exa audit maintain --once             # one cycle; exit 1 if degraded (cron form)
exa audit maintenance-runs            # the last cycles and which step failed
```

The cycle writes no audit event for its own checkpoint (one per cycle would move the head every
time, so "unchanged" would never happen); the checkpoint row, WORM entry and transparency receipt
are the record. A scheduled prune is audited as `audit_pruned`, like a manual one.

## Archival export & retention

Export is **read-only** — the trail is retained in place (append-only), and the export
action is itself audited:

```bash
exa audit export --out audit_2026.json
exa audit export --out old.json --before 2026-01-01T00:00:00
```

### Retention policy: pruning without breaking the chain

By default the trail is kept **forever**. To enforce a minimum retention period set
`EXAMLOPS_AUDIT_RETENTION_DAYS=N`; rows older than N days may then be pruned:

```bash
exa audit prune                                   # dry run (default): what would go
exa audit prune --execute --archive audit-h1.json # delete, after archiving the rows first
```

Deleting a prefix of a hash chain would normally break verification, so a prune is only allowed
when **all** of these hold, and refuses (deleting nothing) otherwise: retention is configured; the
chain verifies now; a fresh signed checkpoint over the head is anchored to the WORM store (or
`--allow-unanchored` was passed and no anchor is configured); the cut itself is anchored; the
deleted rows were written to the archive file, whose SHA-256 is recorded. The newest row is
never pruned, and `--before` can only be *older* than `now - retention`.

The last deleted row's id and hash (the *cut*) are stored in a **signed prune record**
(`audit_prunes`, HMAC with the D7 key). `exa audit verify` starts the retained chain at the cut,
so it still passes — and still **fails** if a retained row is altered, if the prune record is
forged (signature mismatch) or if it is deleted (the chain no longer starts at `GENESIS`). The
prune is recorded in the chain as an `audit_pruned` event. The append-only DELETE trigger is
dropped and recreated inside one transaction, so a failure leaves it in place.

Without the signing key `exa audit verify` still checks the chain but reports the prune record as
*unverifiable* and `fully_verified: false`.

## Streaming to a SIEM

Set `EXAMLOPS_AUDIT_STREAM=1` on every process that writes audit events, and each one is also
published on the [event backbone](event-backbone.md) as `audit.recorded`. The event is enqueued
in the same transaction as the audit row, so it exists exactly when the row does. A SIEM subscribes
to `examlops.events.audit.recorded` (NATS) instead of reading the database.

The event carries what the hash chain covers, exactly as stored: `id`, `ts`, `source`, `actor`,
`action`, `target`, `details` (the stored JSON string), `tenant`, `correlation`, `prev_hash` and
`hash`. So the receiver does not have to trust the pipe. It can check the chain itself:

```python
from examlops.data.audit import verify_audit_stream

problems = verify_audit_stream(events)   # oldest first; [] means intact
```

- An event whose fields were changed on the way fails its own hash.
- A deleted or reordered event breaks the link to its neighbour (`prev_hash`).
- The first event's `prev_hash` is taken on trust, so start from a hash you already hold (a signed
  checkpoint, or the last event you verified).

It is off by default: it doubles the writes of every audited action, and with the default `log`
publisher the relay would copy audit details into the service logs.

## The log view (unchanged)

The familiar log view still works exactly as before:

```bash
exa audit --last 7d
exa audit --model JPCP --action promotion
exa --json audit --last 30d
```

### Reads are ordered by the chain, not by the timestamp

`ts` is `CURRENT_TIMESTAMP`, which has **one-second resolution**, and one retrain or autopilot cycle
writes several events inside a single second. Ordering a read by `ts` alone leaves every tie to the
query plan — and SQLite resolves a tie by scanning the group forward, so `exa audit -n 5` returned
the five *oldest* events of a busy second, in ascending order, presented as the most recent. It was
stable enough to look right.

Every audit read now orders by `ts DESC, id DESC`. `id` is the chain's own order — the sequence
`prev_hash` links together — so "the most recent N events" means the last N in the chain, and the
CLI, the dashboard console and the agent's audit tool all break a tie the same way. Two surfaces
that resolve a tie differently are two different answers to *what happened first*, about the same
incident, to the same reader.

A guard (`tests/unit/test_audit_trail.py`) holds this for any function that reads `audit_events`,
so a fifth surface cannot be added with the old ordering. It is scoped to the **function** doing the
read rather than the file: a module that also samples `drift_snapshots` by `ts` is doing something
legitimate there, and a file-scoped guard flagged exactly that.

## Coverage

Every governed action carries **actor + tenant (D6) + resource** and is chained. Sources across
the platform — CLI, **dashboard**, agent, bridge, control plane — all write through the same
`write_audit_event`, so every event that lands is chained. PII in details should be stored by
reference / redacted (see D8).

### "Complete by construction" was too strong — corrected 2026-09-14

That sentence used to end *"so the chain is complete by construction"*. One writer is not the same
as one guarantee. Around forty call sites deliberately wrap the write in `except Exception: pass`,
because a promotion must not be refused and a secret must not go un-rotated just because the audit
datastore blinked — and until 2026-09-14 a write lost that way left **no trace at all**.

Nothing in this guide could have shown you that, and that is the point worth keeping:

- **`exa audit verify` proves integrity, never completeness.** The chain is recomputed over the rows
  that exist, so an event that never arrived leaves a perfectly valid chain. There is no gap to
  find — a missing link would break the chain, but a missing *event* does not create a missing link.
- The related lesson was already learned once, for rows that are present but **unchained**: `verify`
  counts those rather than skipping them (above). A row that never arrived cannot even be counted.

So the loss is now made visible where it happens:

- it is logged at `WARNING` with the action, target, actor, tenant and cause;
- `examlops.data.audit.dropped_audit_events()` returns the per-action count for the process;
- the control plane publishes that count as `examlops_audit_events_dropped_total{action=…}` on
  `/metrics`, and **AuditEventsDropped** fires on a single lost event
  ([runbook](../runbooks/control-plane.md#auditeventsdropped)).

The last of those was missing until 2026-09-14, and its absence is worth naming: the counter was
documented here and in the EU AI Act guide as *the* signal, and **nothing read it**. A number that
only exists inside a running process, with no metric, endpoint or log line carrying it out, is not
an observable control — you would have had to attach a debugger to a container to see it. The Art.
12 actions are pre-created at zero on the scrape, because an alert cannot fire on a series that
does not exist yet, and the series would otherwise be created by the very outage it exists to
report.

**Treat a non-zero count as a record-keeping incident, not a warning.** Those actions happened and
are absent from the log, so any completeness or coverage statement about that window — including the
[EU AI Act Art. 12](eu-ai-act-compliance.md) coverage figure — is unsound for it.

Two shapes, opposite blast radius, one helper (`audit.audit_best_effort`) for both:

| Where the write sits | What a failure used to do |
|---|---|
| in a `try` body, handler swallows | the event is lost, silently |
| **inside an `except` handler** | the audit error **replaces** the error being handled and escapes |

The second was live in two places. In the autopilot, `run_cycle`'s only outer handler catches
`_RunKilled`, so an unreachable datastore turned one model's handled retrain error into an escape
from the whole cycle, with the run row never updated. In the agent, `trigger_auto_retrain` wrote its
event unprotected **inside a loop over models**, so a failed write aborted the tool with earlier
models already retrained — reporting a failure for an operation that had partly succeeded. A guard
now fails the build on any audit write inside an exception handler, tree-wide.

A third instance was in the **Dataplane bus bridge**, and it is the one with the worst consequence.
Its `retrain_triggered` write sat inside the same `try` as the control-plane POST, whose handler
answers the bus with `error_msg`. So a retrain the control plane had **accepted** was reported back
as failed — and a caller that retries on error fires a *second* retrain of the same model on the
cluster. A lost audit record became duplicate HPC work.

Two surfaces were checked and found already honest, recorded here so the ground is not re-covered:
the dashboard's single `audit_write.audit()` helper logs at `ERROR` **and re-raises** (verified by
running the degraded path, not by reading it), and every MCP tool returns a typed error envelope
rather than an empty result.

### A third shape: the write that stops a loop

The two shapes above are about *where* a failure is absorbed. A third is about *what else is
waiting on it*. An audit write that can raise, inside a loop over items, ends the loop part-way:
earlier items have already been acted on, later ones never will be, and the end-of-run bookkeeping
is skipped.

This is tolerable in a one-shot command — a traceback tells the operator the audit failed, which is
arguably the right outcome. It is not tolerable in an autonomous loop, where nobody is watching.
Measured on 2026-09-14, **eleven** such writes existed, and seven of them were in the autopilot's
own `run_cycle`: an unreachable datastore could end the self-driving cycle with models already
retrained or promoted, and `update_autopilot_run()` never reached, so the run row stayed silent
about actions that had really happened. The others were `exa drift trigger` (×2, inside a loop that
had already claimed each model's cooldown), the dataplane's stream sync, and the telemetry anchor.

All eleven now use `audit_best_effort`.

**And the rule had to widen, because "inside the loop" was the wrong boundary.** Running the cycle
against a refusing datastore — rather than reading it — showed it surviving the loop and then dying
on the `autopilot_cycle_complete` write *after* it. By then every model had been processed and
`update_autopilot_run()` had already recorded the run, but the raise still discarded the result: the
telemetry anchor never ran, the event-backbone publish never ran, and the caller got a traceback
instead of the summary. The two statements immediately below that line are both commented
*"best-effort — must never fail the cycle"*; the audit line between them was not.

So the guard's rule is: **a function that audits inside a loop is a batch operation, and none of its
audit writes may raise.** One sentence, structural, no list of files — and it applies to a batch
function written tomorrow. It caught three more writes in `run_cycle`, including the one above.

### The other half: a write whose failure is hidden

Everything above is about an audit write that **raises** and takes something with it. The mirror
image is a write wrapped in `except Exception: pass`, which takes nothing with it and is therefore
harder to notice: the operation succeeds, the record is gone, and nothing anywhere says so.

That is worse than it sounds, because **neither of the platform's two ways of trusting this log can
see it**. The hash chain proves integrity, not completeness — it is computed over the rows that
exist, so a missing row leaves a perfectly valid chain. And `check_art12_logging` asks whether *at
least one* event of each required type exists, so a dropped one is invisible while any sibling
survives. A window with a lost event looks exactly like a window without one.

`audit_best_effort` is the answer to both halves: it never raises (so a batch survives) and it never
hides (the loss is logged with its cause and counted in `dropped_audit_events()`, which the control
plane publishes as `audit_events_dropped`). **A non-zero count is a record-keeping incident, not a
warning.**

Measured on 2026-09-15, **28** call sites still wrote the event themselves inside a blanket
`except`. Nine are converted — the paths where a missing record is the one an auditor asks for:
secrets, supply-chain signing, **both** policy modules, guardrail blocks, `skipper-watch`'s own
alerts, the data-format upgrade, and the agent's memory governance. The guardrail one also shared a
single `try` with its telemetry insert, so a failed insert skipped the audit write as well; those
are now independent.

Two of those came from re-reading a claim rather than from the sweep. `examlops.policy` and
`examlops.policy_engine` are near-twins with the same private `_audit`, and the first pass converted
only the one whose name came up. And `exa upgrade` argued its silence — *"the upgrade row is the
record"*, which is true: `platform_upgrades` is written by a different module on a different path.
But **a separate record does not make this log complete.** That argument is a reason not to *fail*,
which `audit_best_effort` already honours, not a reason not to *count*.

### What counts as disclosure

Not hiding a loss has more than one honest form, and the guard accepts all of them:

| Handler does | Verdict |
|---|---|
| logs at `WARNING` or above | discloses |
| returns the cause to its caller (`mcp._audit` → *"action succeeded but was not audited: …"*) | discloses |
| re-raises | discloses |
| `log.debug` | **hidden** — invisible at any production log level, which is how the agent's memory audit lost its records |
| `pass` | **hidden** |

The remaining 17 are held by a ratchet in `tests/unit/test_audit_losses_are_recorded.py` that
asserts **equality**, not `<=` — the ceiling must be lowered as sites are converted, or a ratchet
quietly stops meaning anything.

**Converting a site and proving it are two jobs.** A conversion on its own shows only that the call
changed; what matters is that a lost event is *counted*, and nothing demonstrates that except
breaking the audit log and reading the counter. So a second check scans the tree for every function
calling `audit_best_effort` and maps each to the test that breaks the log for it — per **function**,
not per module, because `policy_engine` holds two audit sites and only the decision one was tested
while the bundle one looked covered by a per-module tally. Three sites predating this work have no
**declared** drop-counting test and are held by their own ratchet — down from eleven as they are
read one at a time. The three that remain (`exa drift trigger`, the autopilot's anomaly classifier,
and the dataplane's stream sync) all sit behind gates that make a unit test reach for a real MLflow
or control plane; they are waiting on an isolated fixture rather than on the audit work itself. The ones taken so far are the ones an investigation starts from: the sysadmin
**approval** decisions (who approved what, and when), `retrain_triggered` and the eval gate's
calibration refusal (both Article 12 required events), and the **telemetry anchor**, which is what
makes rows outside the hash chain tamper-evident in the first place.

*Declared* is the careful word. Absence from that mapping means it names no test, **not** that no
test exists — a distinction that cost a wrong claim once already, when `autopilot_cmd.run_cycle`
turned out to be proved by `test_autopilot.py` all along. Nor is a file that merely mentions both
a function and `dropped_audit_events` evidence: co-occurrence is not coverage, so an entry is added
only after reading the test. The mapping can check that a test it names still **exists**; it cannot
check that the test proves anything, which is why entries are read in rather than assumed. A narrow `except ImportError: pass` falling through to a documented
alternative is not flagged: the dashboard's own `audit_write` uses exactly that shape while logging
and re-raising everything else. The scan resolves aliased imports, because a mutant that wrote
`from ... import write_audit_event as _w` slipped past an earlier name-matching version of it.

### Four doors to one action

`retrain_triggered` is written by the CLI, the agent, the dashboard and the bridge. Each was a
separate call site with its own error handling, and the first three sweeps each found a different
subset — which is the argument for the guard deriving its own scope rather than listing files:

| Door | Was |
|---|---|
| `exa retrain` | silent `except Exception: pass` |
| Skipper (`skipper/tools/training.py`) | silent `except Exception: pass` |
| Dashboard (`routers/pipelines.py`) | already honest — logs and re-raises |
| Dataplane bus bridge | reported the loss to the caller as a failed retrain |

> The dashboard was **absent from that list until 2026-09-02**, and so was its chaining: sixteen
> routers and five modules wrote raw `INSERT`s, so every dashboard mutation would have landed
> outside the chain and invisible to `verify`. Dashboard code now goes through one
> `audit_write.audit()`
> helper, a guard test fails on any new raw insert, and `verify` counts what it cannot check. A
> caller already inside a write transaction passes its connection
> (`write_audit_event(..., conn=…)`), which both avoids a second-connection deadlock and makes the
> audit atomic with the mutation it records.

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
