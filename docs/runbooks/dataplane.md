# Runbooks: dataplane

Alerts from the `examlops-dataplane` group: the data-integration service that pulls configured
sources (databases, object stores, repositories, REST and streams) on a schedule and commits
content-addressed snapshots that training pins (ADR 0130).

The commands for every dataplane alert:

```bash
exa dataplane pulls --source <source>   # recent pulls of a source, newest first, with errors
exa dataplane test <source>             # is the source reachable and are the credentials valid?
exa dataplane pull <source>             # pull now and commit a snapshot
exa dataplane snapshots <source>        # what has been committed
```

The service's port `18010` is published on loopback unless `DATAPLANE_BIND` says otherwise.
With no `DATAPLANE_TOKEN` and no identity federation its reads are open, so widen the bind only
together with a real token.

## DataplaneDown {#dataplanedown}

**Meaning:** Prometheus has not reached the dataplane service for 2 minutes.

**Impact:** scheduled pulls are not running, so sources go stale. Training on already committed
snapshots is unaffected.

**Check:** `exa stack status` (the `dataplane` service), then
`exa stack logs --service dataplane --tail 200`.

**Fix:** restart the service; the logs name what stopped it. Pulls that were due run on the next
schedule tick. `exa dataplane pull <source>` runs one now.

## DataplaneSourceStale {#dataplanesourcestale}

**Meaning:** a *scheduled* source (the `source` label) has had no successful pull for more than
twice its own schedule, sustained for 30 minutes: a `1h` source after 2 hours, a `@daily` one after
2 days. The threshold is `dataplane_source_schedule_seconds`, which the service publishes only for
an enabled source with a schedule, so a source you pull only on demand, or have disabled, never
raises this alert.

**Impact:** retrains that use the source's latest snapshot train on data that is old.

**Check:** `exa dataplane pulls --source <source>`: failing pulls (see
[DataplanePullFailing](#dataplanepullfailing)), or no pulls at all (the schedule, or the service,
is not running).

**Fix:** fix the failing pull, or the schedule. `exa dataplane pull <source>` pulls now and
commits a snapshot if the data changed. The metric measures how long ago a pull last *succeeded*,
not how old the data is: a pull that finds nothing new reports the source `unchanged`, commits no
new snapshot, and still resets freshness to zero. A source whose data legitimately does not change
therefore stays fresh as long as its scheduled pulls keep succeeding; this alert means the pulls
themselves stopped.

## DataplanePullFailing {#dataplanepullfailing}

**Meaning:** the latest finished pull of a source failed (an `unchanged` pull counts as success),
sustained for an hour.

**Impact:** the source goes stale; see above.

**Check:** `exa dataplane pulls --source <source>` shows the error. `exa dataplane test <source>`
separates reachability and credentials from the data itself.

**Fix:** by cause: credentials that expired or rotated (update the source's connection),
the system down or unreachable from the service's network, or the data itself (a schema change the
connector refuses; `exa dataplane preview <source>` shows what it reads).

## DataplaneCatalogUnavailable {#dataplanecatalogunavailable}

**Meaning:** the dataplane service could not read its own source catalog for 10 minutes. This is
not the same failure as [DataplanePullFailing](#dataplanepullfailing), which is about one source's
pulls. Here the catalog itself, in the shared platform datastore, is unreachable, so the service
cannot list any source and every per-source gauge disappears at once.

**Impact:** no per-source pull status or freshness is visible, and the scheduler cannot tell which
sources are due, so scheduled pulls stop. Snapshots already committed are unaffected, and training
runs that pin one keep working.

**Check:** `exa stack logs --service dataplane --tail 200 --no-follow` shows the datastore error.
`exa stack status` shows whether Postgres is up (when `EXAMLOPS_DB_BACKEND=postgres`), and
`exa instance check` checks the data root and datastore. `dataplane_catalog_up` on the service's
`/metrics` stays 0 while the read fails and returns to 1 on the next successful scrape.

**Fix:** restore the platform datastore (Postgres reachability, disk space, permissions). No
dataplane-specific action is needed once the datastore answers again — the next scrape reads
`dataplane_catalog_up=1` and the per-source gauges resume with it.
