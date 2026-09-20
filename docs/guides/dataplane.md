---
description: "Pull SQL, object-storage, Zenodo, REST and Kafka data into content-addressed snapshots, then pin a training run to an exact one — the dataplane connectors, service, scheduler and security model."
---

# The dataplane

The dataplane is ExaMLOps's data-integration layer: it pulls data that lives *outside* the
platform (a database, an object store, a public repository, a REST API, a Kafka topic) into
versioned, content-addressed snapshots that training pins to. It is a separate concern from the
*serving plane*, which is the request/inference path — see
[Data versioning & reproducibility](data-versioning.md) for how a resolved revision is recorded
against an MLflow run.

## What it is

Three phases, always in this order:

- **Connect** — a [Named Connection](projects-workspaces.md#9-named-connections) holds the
  endpoint and credentials; a *source* names a connector kind, an optional connection, and a spec
  (what to read from it).
- **Pull** — a connector turns `(connection, spec, watermark)` into bounded Arrow batches. A
  successful pull writes Parquet parts to the snapshot store, then a manifest, then a revision
  pointer, then `_latest` — the last write is the commit, so a reader never sees a half-written
  snapshot.
- **Pin** — a model's YAML binds a dataset entry to a source. A training run resolves one
  revision at flow start and every build inside that run — training, the data-contract gate, the
  MLflow tag — uses that same materialized copy, even if the source's `_latest` moves meanwhile.

Credentials never leave the connection: a training process, an HPC node, or a plugin never
receives one directly.

## Connect a source

Register the connection first (once per credential), then the source (once per dataset).

```bash
exa connection create lab-pg --kind sql \
    --config '{"url": "postgresql+psycopg://reader@db.lab/jobs"}' \
    --secret-value "$LAB_PG_PASSWORD"
exa dataplane sources create pm100 --connector sql --connection lab-pg \
    --spec-json '{"table": "jobs"}'
```

Every built-in connector kind, with a representative spec:

**`sql`** — any SQLAlchemy URL, streamed read-only (a transaction-level `READ ONLY` where the
dialect supports it, plus a statement timeout):

```bash
exa dataplane sources create pm100 --connector sql --connection lab-pg \
    --spec-json '{"table": "jobs", "incremental": true, "watermark_column": "job_id"}'
```

An incremental `sql` source is **insert-only**: each pull appends the rows past the watermark to
everything the previous snapshot held, so `watermark_column` must increase monotonically and be set
once per row — an increasing id or a `created_at`. A column that moves when a row is *updated*
(`updated_at`) re-reads the updated row and the snapshot then holds it twice; pull such a table
without `incremental` (see [Incremental pulls](#incremental-pulls)).

**`files`** — object storage or HTTP(S), one URL plus a format. The connection carries the
credentials (`access_key`/`secret` for S3, `account_name`/`secret` for Azure Blob, a bearer token
for HTTP); the source spec carries the location:

```json
{"url": "s3://data-lake/pm100/*.parquet", "format": "parquet"}
{"url": "gs://data-lake/pm100/*.parquet", "format": "parquet"}
{"url": "https://facility.example/exports/latest.csv", "format": "csv"}
{"url": "sftp://ingest.example/drops/*.jsonl", "format": "jsonl"}
```

The connection's kind fixes which URL schemes a source may use with it, so a credential is never
sent over a protocol it was not issued for: `s3` → `s3://`; `fs` → `gs://`, `gcs://`, `abfs://`,
`az://`, `sftp://`, `hdfs://`, `file://`; `uri` → `http://`, `https://`. A mismatch is refused.
A `uri` connection's bearer token is sent only to the origin (scheme, host, port) of its own `uri`
— a source URL on any other origin is still read, but anonymously. An `sftp://` source's password
goes only to the connection's own `host` (and `port`); the host is egress-checked like any other,
and its SSH host key must already be known — the service user's `~/.ssh/known_hosts` plus
`EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS` — unless `EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS=1`. The service's own
SSH agent and keys are never offered to a source's host.

What this means when you configure one:

- A `uri` connection sends its bearer token only when the connection names its own URL (`uri`,
  `url` or `base_url` in its config). **A connection without its own URL sends no credential at
  all**, whatever the source's URL — set the URL on the connection, not only on the source.
- **An SFTP password connection must set `config.host`** (and `config.port` if it is not 22). A
  source whose URL names another host, or a password connection with no host, is refused.
- **An unknown SSH host key is rejected.** Add the server's key to the service user's
  `~/.ssh/known_hosts` or to a file named by `EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS`;
  `EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS=1` accepts unknown keys, for development only.
- **The service's own SSH agent and key files are never used** for a source: an SFTP source
  authenticates only with its connection's credential.

S3 — for sources and for the snapshot store alike — goes through pyarrow's own S3 filesystem, so
there is no `s3fs` to install: the PyPI pyarrow wheels already include S3 support. An S3
connection's `endpoint` is `scheme://host[:port]` (`http://minio:9000`,
`https://s3.example.org`) and is checked against the egress allow-list before anything is
sent: a loopback, private or platform-internal host is refused unless allow-listed — a `files`
source on the platform MinIO (`http://minio:9000`) needs `EXAMLOPS_DATAPLANE_ALLOWED_HOSTS=minio`.
The `sql` and `kafka` connectors are checked the same way, on the connection URL's host and on
every `bootstrap_servers` entry respectively — a `sql` connection to the platform Postgres
(`postgresql+psycopg://…@postgres/mlflow`) needs `EXAMLOPS_DATAPLANE_ALLOWED_HOSTS=postgres`,
and a `kafka` connection to a broker named `kafka` needs `EXAMLOPS_DATAPLANE_ALLOWED_HOSTS=kafka`.
An `http://` endpoint is pinned to the address the check approved (so the request's `Host` is that
IP — a name-based virtual host behind an ingress will not answer it); an `https://` one keeps its
hostname for TLS. This check is weaker than the one on HTTP sources: it covers only the **first
hop**, an `https://` endpoint is not pinned against DNS rebinding, and pyarrow's S3 client follows
redirects to any host. Allow-list only S3 endpoints you trust, and give the dataplane container a
network egress policy. The connection may carry a `region` (`eu-west-1`). A connection with no `access_key`/`secret`
reads anonymously; a source never borrows the service's own AWS credentials. The region is
otherwise `AWS_REGION`, then `AWS_DEFAULT_REGION` (else `us-east-1`, which MinIO ignores), so set
one for an AWS bucket in another region. Buckets are never created — the store's bucket must
already exist (`minio-init` creates `EXAMLOPS_DATA_BUCKET` in Compose).

The snapshot store itself is operator configuration: its endpoint is not egress-checked, and with
no `EXAMLOPS_DATA_S3_ACCESS_KEY`/`AWS_ACCESS_KEY_ID` it uses the AWS default credential chain
(`~/.aws`, IRSA, an instance role). Off-cloud with no keys, that chain spends several seconds
probing the EC2 metadata endpoint on every store construction — set the keys, or
`AWS_EC2_METADATA_DISABLED=true`.

A `file://` URL is refused unless `EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES=1` — see
[Security](#security).

**`zenodo`** — a public record id, checksum-verified on every download:

```bash
exa dataplane sources create pm100-zenodo --connector zenodo \
    --spec-json '{"record": 10127767}' --schedule 1d
```

**`rest`** — a paginated JSON API. The connection's `base_url` plus a spec path, pagination
style (`none`/`page`/`offset`/`cursor`/`link`) and, for an incremental source, a watermark field:

```bash
exa connection create weather-api --kind rest --config '{"base_url": "https://api.example/v1"}' \
    --secret-value "$WEATHER_API_KEY"
exa dataplane sources create weather --connector rest --connection weather-api \
    --spec-json '{"path": "observations", "pagination": {"type": "page"}, "incremental": true, "watermark_field": "observation_id", "since_param": "since_id"}'
```

As for `sql`, an incremental `rest` source is insert-only: `watermark_field` must be a monotonic,
set-once field (an increasing id or a creation time), never one that changes when a record is
updated.

**`kafka`** — a bounded batch read from a topic (earliest/latest or an explicit offset/timestamp
range — never an unbounded consumer):

```bash
exa dataplane sources create clickstream --connector kafka --connection kafka-prod \
    --spec-json '{"topic": "clicks", "start": "earliest", "end": "latest-at-start"}'
```

Register many sources at once, GitOps-style, from a YAML file (credentials are refused inline —
only a Named Connection):

```bash
exa dataplane sources apply --file sources.yaml --dry-run
exa dataplane sources apply --file sources.yaml
```

Check what a connector's dependencies look like on this install, and what a source will actually
read, before committing to anything:

```bash
exa dataplane connectors
exa dataplane test pm100
exa dataplane preview pm100
```

## Pull and inspect

```bash
exa dataplane pull pm100
exa dataplane pull pm100 --full
exa dataplane pulls --source pm100
exa dataplane snapshots pm100
exa dataplane manifest pm100 latest
```

A pull that finds nothing new past the watermark reports `unchanged` rather than committing an
identical snapshot; freshness (below) still resets to zero.

`pulls` and `snapshots` answer different questions, and their limits count different things.
`pulls` lists attempts, successful or not. `snapshots` lists what a training run can pin to — a
committed pull that produced a revision — and its limit counts **snapshots**, so a source whose
recent pulls have been failing still shows its revisions. This matters precisely when it is easiest
to misread: a broken source is when someone looks, and "no snapshots" would say the source never
produced data rather than that it is failing *now*. The revisions stay pinnable throughout, and
`exa dataplane pulls --source <name>` is where the failures are.

Ask the dataplane service to run a pull instead of running it in-process:

```bash
exa dataplane pull pm100 --remote
```

### Incremental pulls

A source with `"incremental": true` carries the previous snapshot's parts forward and adds only
what is new. "New" is deliberately narrow, because anything carried is never re-read:

- **`files` and `zenodo`** append only files the previous snapshot has not seen. If a file it
  already holds changed (a new ETag, modification time, size or Zenodo checksum) or disappeared,
  the pull re-reads the whole source instead, so a file corrected in place never ends up in the
  snapshot twice. An HTTP(S) source is a single file: a changed body is always a full re-read.
- **`sql` and `rest`** are insert-only: the watermark column/field must be monotonic and set
  once per row (see the examples above).
- **A changed spec** (another table, query, URL or glob) always makes the next pull a full one.
  If that full read finds exactly the data already held, the pull still commits a new snapshot —
  same revision, new spec — so the pulls after it are incremental again.
- `exa dataplane pull <source> --full` ignores the watermark on demand.

Why a pull ran in full is written to the dataplane log, and to the `dataplane_pull_succeeded`
audit event when the pull commits a new snapshot.

### Pruning

Old snapshots accumulate; `exa dataplane prune` deletes them, but always keeps the newest N, the
current `_latest`, and any revision an MLflow run actually used:

```bash
exa dataplane prune pm100 --keep 5 --dry-run
exa dataplane prune pm100 --keep 5
```

Prune takes the same per-source lock as a pull, so it refuses (and changes nothing) while a pull
of that source runs — run it again afterwards. A pull holds its lock for as long as it runs: the
lease is renewed every third of its TTL, so a slow pull is never mistaken for a crashed one. A pull
directory is only treated as abandoned when its newest object is more than an hour old.

Prune also refuses to run blind. Which revisions a training run used is recorded in
`platform.db`; if the store has snapshots of the source but the catalog has no revision rows for
it — a lost or freshly restored `platform.db` — prune stops and asks you to run
`exa dataplane catalog-rebuild` first. `--force` overrides that, and then **nothing a training
run used is protected** beyond the newest N and `_latest`.

### Rebuilding the catalog

The store is self-describing — every snapshot's manifest is enough to reconstruct the pull
history — so a lost or restored `platform.db` is not a lost history:

```bash
exa dataplane catalog-rebuild --dry-run
exa dataplane catalog-rebuild
```

`catalog-rebuild` walks the store, not the catalog: every project and source prefix, every pull
with a committed manifest. It restores each committed pull's row (`succeeded`, with its
revision, row and byte counts, and times from the manifest) and records every committed revision
in the revision index — not only the latest. Running it again changes nothing. Re-indexing
history is not news: models built on the dataset go stale only if the rebuild finds a revision
newer than every one the catalog already had (and then only once per source). Two things are not
in the store and cannot be rebuilt:

- **Source definitions.** A manifest records the connector, the connection name and a hash of the
  spec, never the spec itself. Sources that have snapshots but no definition are listed as
  *sources to re-register*; register each again with `exa dataplane sources create` (or
  `sources apply`).
- **Which training run used a revision.** A run links itself to its revision when it trains, so
  links made before the loss are gone until a run uses that revision again. Until then,
  `prune` cannot protect those revisions — keep `--keep` generous after a rebuild.

## Train on a snapshot

Bind a dataset entry to a source in the model's YAML:

```yaml
datasets:
  - name: PM100Dataset
    backend: dataplane
    dataplane:
      source: pm100
      tables:
        default: jobs
```

Then train against it:

```bash
exa pipeline run --model JPCP --dataset PM100Dataset --backend dataplane
```

The run resolves `pm100`'s `_latest` revision once, materializes it to a local cache directory,
and reuses that exact copy for every build the run needs — the data-contract gate, the training
loop and the MLflow `dataset_revision` tag all see the same files. Reproduce an exact past run by
pinning the revision it used instead of resolving `_latest` again:

```bash
exa pipeline run --model JPCP --dataset PM100Dataset --backend dataplane --dataset-revision <rev>
```

`<rev>` is the full 64-character revision id (`exa --json dataplane snapshots <source>` prints
it); `latest` means the current `_latest`, and anything else is refused. The revision id is a
content address: before a pinned snapshot is downloaded, its manifest's file digests are hashed
again and must reproduce that id, and every downloaded file must match its digest. Data swapped
behind a pinned id is refused, not trained on.

**What an HPC node needs.** A node that only trains never talks to `platform.db` — it needs the
`examlops` package with the `dataplane-files` extra (`pip install 'examlops[dataplane-files]'`,
or `[dataplane-sql]`/`[dataplane-kafka]` for those connector kinds) and the same dataset-store
environment (`EXAMLOPS_DATAPLANE_STORE_URL` or the `EXAMLOPS_DATA_S3_*`/`EXAMLOPS_DATA_BUCKET`
fallback) as the submitting host, so it can re-resolve the pinned revision and materialize it into
its own local scratch (`EXAMLOPS_DATAPLANE_CACHE_DIR`). No dataplane service, no database.

## Schedules, limits and contracts

A source's `--schedule` (`15m`, `6h`, `1d`, `@daily`) is what the dataplane **service**'s
scheduler reads — the CLI's own `exa dataplane pull` always runs immediately regardless of a
configured schedule. `--max-rows`/`--max-bytes` on `sources create` cap one source's pulls;
platform-wide caps (`EXAMLOPS_DATAPLANE_MAX_ROWS`/`_MAX_BYTES`/`_MAX_SECONDS`) bound every
source regardless of its own setting. `--contract` names a
[data contract](data-quality.md) checked against the materialized snapshot before a training run
commits to it.

A contract is checked one table at a time, and tables are never concatenated. A contract that sets
`table` checks only that table; one that sets no table is checked against each table on its own.
The training gate on a pinned snapshot checks the contract's `table`, or else the tables the
model's `datasets[].dataplane.tables` maps, or else every table. Each check reads at most
`EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS` rows of the table (default 200000), streamed part by part,
so a large snapshot never has to fit in memory. When a table is larger than that, the check runs
on a sample. The pull's result, its audit record and the gate's report then show `sampled`, with
`rows_checked` of `rows`. `min_rows` still judges the table's real row count.

## Security

- **Egress allow-list.** A connector may only reach a public address, or a host explicitly listed
  in `EXAMLOPS_DATAPLANE_ALLOWED_HOSTS` (hostnames and/or CIDRs) — a platform-internal name like
  `mlflow` or `minio` is refused by default. For HTTP, REST and Zenodo sources the check is pinned
  to the resolved IP and repeated on every redirect, so DNS rebinding and redirects cannot bypass
  it; the guarded HTTP backend also **refuses unix sockets outright**, since an address guard cannot
  judge one and `/var/run/docker.sock` is what it would otherwise reach. **`sftp` pins too**: it
  connects to the address the check approved and hands paramiko that socket plus the *name*, which
  is what `known_hosts` entries are keyed on — so there is no second lookup between the check and
  the connection, and host-key verification still applies to the name. S3 endpoints are checked on the first hop only (see *Connect a source*); a network egress
  policy on the dataplane container is the backstop for them. The `sql` connector checks the
  connection URL's host before the engine is built; for `postgresql+psycopg` the checked address
  is additionally pinned via libpq's `hostaddr`, so DNS cannot rebind between the check and the
  connection while TLS still verifies the hostname — other dialects are checked but not pinned.
  The `kafka` connector checks every `bootstrap_servers` entry before the Consumer is built; one
  denied entry refuses the whole connection. Residual for both: a Postgres redirect-equivalent
  does not exist, but a Kafka broker's own metadata can advertise other listener addresses that
  librdkafka then connects to directly — those later hops are not re-checked, so a network egress
  policy is the backstop there too.
- **Local files are gated.** A `file://` source/sink, or a `sqlite:` URL on the `sql` connector
  (which has no network host to check), is refused unless
  `EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES=1` — the dataplane service's own container should never
  read platform state through a connector. A database reached over a unix socket takes the same
  gate: write the socket as a host-less URL (`postgresql+psycopg://reader@/jobs`, libpq's default
  socket directory). A query parameter that names a host or socket (`?host=`, `?unix_socket=`, …)
  is always refused, because it would route the connection past the host the guard checked.
- **Read-only database roles.** The `sql` connector sets a read-only transaction and a statement
  timeout where the dialect supports it (Postgres, MySQL/MariaDB, SQLite); the connection's own
  database role should still be least-privilege and read-only.
- **Credentials never leave the connection.** A spec, manifest, error message or audit record
  carries a source or connection *name*, never a secret value — every error string that could
  carry one is redacted before it crosses a process boundary. A credential-shaped spec key
  (`Authorization`, `X-API-Key`, `Cookie`, `auth`, `credentials`, `token`, … at any depth,
  `spec.headers` included) is refused when the source is defined; inside `spec.headers` any name
  containing `key`, `token`, `auth`, `secret`, `sig`, `session`, `cookie`, `jwt`, `password` or
  `credential` is refused (`X-Auth-Key`, `Ocp-Apim-Subscription-Key`, `X-Amz-Signature`,
  `session_id`, …). A URL anywhere in a spec may name a user (`sftp://ingest@host/…`) but never a
  password (`https://user:pw@host/…`), nor carry a credential in its query (a presigned
  `X-Amz-Signature`, `?token=`, `?access_token=`, `?key=`, `?code=`, …) — both are refused too. A
  connection's secret goes only
  to the server the connection names: neither a source spec nor a server's response (a Zenodo
  record's file links) can redirect it — see *Connect a source*.
- **The service token.** `DATAPLANE_TOKEN` is the dataplane service's static bearer. Its effect
  depends on exactly how it is set, and only one of these four states is safe to run beyond
  loopback:
  - **unset** (no identity federation either) — the loopback-only default: reads are open, every
    write returns 503. Set a real token on any deployment reachable beyond loopback.
  - **set to a placeholder or fewer than 16 characters** (`changeme`, `…-change-me-…`, and
    similar) — treated as configured-but-broken, not as unset: with no working federation
    *every* route but `/health`/`/ready`/`/metrics` returns 503, reads included.
  - **set to a real secret** — every route but those three needs `Authorization: Bearer
    <token>` (401 missing, 403 wrong).
  - **identity federation configured** ([ADR 0120](identity-federation.md), `EXAMLOPS_IAM_CONFIG`)
    — tokens from the trusted data-center IdP are accepted alongside the static one: any mapped
    role can read, `operator` and above can write, and the center's PDP may still veto the call.

  **Projects.** With `EXAMLOPS_MULTITENANCY` on, an IdP caller is also limited to the projects it
  belongs to. Reading a project's source (show, snapshots, a pull's status, the source list) needs
  `viewer` on `project:<p>`, and changing or using it (create, delete, test, preview, pull) needs
  `editor`. Grant it to the caller's federated id (`<provider>:<subject>`, as `exa auth whoami`
  prints it), for example `exa project add-member acme center:alice --role editor`, or have the
  IdP carry it as a project role in the token. A global source (no project) is readable by any role and writable only from `operator`
  up. The source list shows only what the caller may read. A refusal is a plain `403` that says
  nothing about the other project, not even whether the source exists there. The static
  `DATAPLANE_TOKEN` is the platform credential and keeps full access, and with multitenancy off
  nothing changes. The center's PDP is asked with the source's real project as the resource.

  `/health` reports the effective mode (`open`, `static`, `federated`, `static+federated`,
  `token-invalid`, `trust-file-invalid`, …), and the service logs it once at startup.
  `EXAMLOPS_DATAPLANE_TOKEN` is the client side, used by `exa dataplane pull --remote`.
- **A dedicated object-store credential.** The compose deployment gives the dataplane service its
  own least-privilege MinIO credential (`MINIO_DATAPLANE_ACCESS_KEY`/`_SECRET_KEY`), scoped to the
  dataset bucket only — never the platform's root MinIO credential.

## The service

`platform/services/dataplane` wraps the same library the CLI uses behind an HTTP API, an interval
scheduler that fires each source on its own `--schedule`, and Prometheus metrics — so a pull can
run unattended instead of only from an operator's `exa dataplane pull`.

```bash
curl http://localhost:18010/health
curl http://localhost:18010/metrics
```

`/health` reports whether the catalog and snapshot store are reachable, which connectors have
their dependencies installed, and the effective auth mode. `/metrics` exposes, per source,
`dataplane_source_freshness_seconds` (seconds since the source's last successful pull — an
`unchanged` pull counts as success and resets it to zero) and `dataplane_source_up` (1 if the
source's last finished pull committed, 0 if it failed *or* if that source's own catalog read
failed this scrape), plus `dataplane_pull_total`/`dataplane_pull_duration_seconds`/
`dataplane_rows_total`/`dataplane_bytes_total` for every pull the service has run. A fifth,
label-free gauge, `dataplane_catalog_up`, is 1 if the whole source catalog answered this scrape
and 0 if it did not — it exists because `dataplane_source_up` only has a series per already
*registered* source, so it cannot tell a healthy install with zero sources from one whose catalog
cannot be read at all. `dataplane_source_schedule_seconds` is a scheduled source's pull interval.
It is published only for an enabled source whose schedule parses, and `DataplaneSourceStale` fires
when freshness passes twice that value. Alerts on these metrics (`DataplaneDown`, `DataplaneSourceStale`,
`DataplanePullFailing`, `DataplaneCatalogUnavailable`), and how to act on each one, are in the
[dataplane runbook](../runbooks/dataplane.md).

`exa dataplane pull <source> --remote` is the CLI-side view of the same API — see
[Pull and inspect](#pull-and-inspect) above.

## Live streams

Everything above is the batch half of the dataplane: connect, pull, pin. This section covers the
other half: live inference requests, served one at a time instead of pulled on a schedule.

### What a stream is

A stream is a named binding of one inbound connector to one project's model and alias, held in
the `dataplane_streams` catalog. A source turns `(connection, spec, watermark)` into a snapshot
that training pins to later; a stream turns one inbound message into one inference call, answered
(or dead-lettered) as it arrives. A stream never writes to the snapshot store, and it has no
schedule: it runs continuously while its connector is up.

### The two ways in

Two connector kinds exist today:

- **`http`** is served synchronously by the dataplane service itself. `POST
  /streams/{name}/messages?project=` pushes one message and gets one answer in the same call.
- **`kafka`** is a long-running consumer-group member that reads a topic and, when
  `options.reply_topic` is set, answers on it.

Two things some readers may expect are not here yet: a Dataplane bus req/res connector, and any `exa`
command for streams. There is also no HTTP route yet to register a stream. The write path exists
in the library (`examlops.dataplane.streams.bindings.define_stream`, tenancy-checked and audited),
but nothing outside the pack sync calls it in this release. In practice, today, you define a
stream in the active pack's model YAML, under `inference.streams`. The stream supervisor reads
that list on its own: once at startup, then at most once a minute after that (it also re-syncs
whenever the pack changes on disk).

### Defining a stream

A worked example, an HTTP push stream and a Kafka stream on the same model:

```yaml
# usecases/<pack>/models/JPCP.yaml
inference:
  streams:
    - name: jpcp-live
      connector: http
      alias: Production
      options:
        passthrough: ["num_nodes"]

    - name: jpcp-live-kafka
      connector: kafka
      alias: Production
      address: jpcp.requests
      connection: kafka-prod
      options:
        reply_topic: jpcp.replies
        dlq_topic: jpcp.dlq
        dlq_store_payload: false
        start: earliest
        allow_alias_override: false
      limits:
        max_in_flight: 64
        rate_per_min: 0
        deadline_ms: 2000
        max_bytes: 1048576
        max_attempts: 5
      state: enabled
```

Top-level fields:

- **`name`** and **`connector`** are required. `connector` is `http` or `kafka` (a third-party
  stream connector, once one is installed, is named the same way).
- **`address`** is the Kafka topic name. It is ignored for an `http` stream: the push route
  matches on the stream's own `name` in the URL, not on this field.
- **`connection`** names a Named Connection. A `kafka` stream needs one of kind `kafka`; an
  `http` stream needs none (it has no outbound connection of its own).
- **`alias`** is the MLflow alias the stream serves (`Production`, `Canary` or `Staging`).
  Defaults to `Production`.
- **`state`** is `enabled`, `paused` or `disabled`. Defaults to `enabled`.

`options` (all optional):

- **`passthrough`**: a list of payload keys copied into the model request verbatim, in addition
  to the fields the model's own input schema asks for. This is what lets a message carry a field
  a model needs but does not declare (`num_nodes`, say) without the request being rejected.
  Default: none.
- **`reply_topic`** (`kafka` only): the topic a reply is published to. With none set, a Kafka
  stream never publishes an answer; it only consumes.
- **`dlq_topic`** (`kafka` only): a topic a dead-lettered message's raw bytes are also copied to,
  with headers naming the error, the stream and the attempt count. Independent of the database
  dead-letter record described below, which is always written regardless of this setting.
- **`dlq_store_payload`**: whether a dead letter's payload is stored in the database (see
  [Operating it](#operating-it)). Default: `false`. Metadata (reason, error, size, digest, origin)
  is always stored either way.
- **`start`** (`kafka` only): `earliest` (default) or `latest`. It applies only to a partition
  the consumer group has never committed an offset for: `latest` starts it at the current log end
  instead of replaying history. A partition that already has a committed offset always resumes
  from it, whatever this says.
- **`allow_alias_override`**: whether a caller may name an alias other than the binding's own.
  Default: `false`. See [The request path](#the-request-path) for why this defaults off.

`limits` (all optional):

- **`max_in_flight`**: concurrent requests admitted at once for this stream. Default `64`.
- **`rate_per_min`**: a per-minute cap; `0` (the default) means unlimited.
- **`deadline_ms`**: the time budget handed to the model service. `null` (the default) means no
  stream-imposed deadline; a caller's own budget (an HTTP header, on push) can still apply.
- **`max_bytes`**: the largest message body admitted. Default `1048576` (1 MiB); an HTTP push
  is additionally capped by the service-wide `EXAMLOPS_DATAPLANE_PUSH_MAX_BYTES`, whichever is
  smaller.
- **`max_attempts`**: how many times a Kafka message is retried before it is dead-lettered as
  `retries_exhausted`. Default `5`. Push has no attempts of its own: the caller retries and the
  `Idempotency-Key` header keeps a retry from being served twice.

### Tenancy

A stream's project is not a setting of its own: it comes from the model. If the model's YAML has a
top-level `project:` key (the same key [project-scoped serving](projects-workspaces.md) already
uses), that is the project. If it does not, the project the model is **assigned** to is used:

| the model's YAML | the model's project membership | the stream's project |
|---|---|---|
| `project: research` | must include `research` | `research` |
| no `project:` key | exactly one project | that project |
| no `project:` key | no project at all | `_global` (unscoped) |
| no `project:` key | more than one project | refused — add `project:` to the YAML |

The last row is an error on purpose: with several candidates the platform will not guess, and the
message names the model, never the projects. This matters because most packs carry no `project:`
key at all while their models *are* assigned to projects — with only the YAML key consulted, every
stream such a pack declares would be refused.

A stream in project `P` may bind model `M` only if `M` actually belongs to project `P` (checked
against the project's own model membership, not merely echoed from the YAML); a stream with no
project may only bind a model that belongs to no project at all. A stream naming a model outside
its own project's membership is refused when it is defined. Every refusal is logged (one line per
refused entry, naming the file and the entry, never its values), and a sync that reports any error
skips the removal sweep for that run and says so in the log — one broken entry must never look
like "every other stream was intentionally removed".

**A project-scoped stream needs a connection in its own project.** A Named Connection is looked up
by `(project, name)` exactly, with no fallback to an unscoped one — for streams and for [batch
sources](#connect-a-source) alike. A `kafka` stream in project `research` therefore cannot use a
`_global` connection named `kafka-prod`; create one in `research`
(`exa connection create kafka-prod --kind kafka --project research …`). The error it would
otherwise report (`connection 'kafka-prod' not found (project=research)`) is exact but says
nothing about why, so check the project first.

A stream declared in a model YAML belongs to the pack (`origin: pack`) and cannot be changed
through the API (there is none yet, but the rule already holds for the library's write path):
edit the YAML instead. The reverse holds too: a pack sync never overwrites a stream an operator
defined some other way. Removing a stream's entry from the model YAML does not delete it. The next
sync disables it, remembering whether it was `enabled` or `paused` beforehand, and restores that
same state if the entry comes back later. A stream a human already disabled, for any other reason,
is left alone by the sync either way.

### The request path

Every message, whichever connector delivered it, takes the same path: check the model and alias,
validate the payload and build the request body, admit it (in-flight and rate limits), call the
model service, then answer. Only after the answer is on its way does the stream count it: first
telemetry, then drift.

The binding's **model and alias are authoritative**, never the caller's. A message naming a
different model is rejected outright; a message naming a different alias is rejected too, unless
`options.allow_alias_override` is `true` and the name is one of `Production`, `Canary` or
`Staging`. This defaults off on purpose: a caller must never be able to choose which model
*version* answers its request, nor which alias's drift window its prediction lands in. A canary
producer that could name `Production` would push that window over its threshold with traffic
nobody meant to count there.

**`model` and `alias` are read from the envelope only.** A message is an envelope when its top
level has a `payload` object; then its sibling `model`/`alias`/`metadata` keys are the override,
checked against the binding. A message *without* a `payload` key is itself the payload, so a
top-level `alias` there is a field of your data, not an override: it is passed to the model and
the request is served by the bound alias, answering `200` where the envelope form would have
answered `422`. If you mean to name an alias, send an envelope — the silent case is a
mis-typed override, not a refused one.

```json
{"payload": {"embedding": [0.1, 0.2]}, "alias": "Staging"}   // an override — checked, 422 if refused
{"embedding": [0.1, 0.2], "alias": "Staging"}                // data — served by the bound alias
```

Only two outcomes ever feed drift: `model` (the model itself failed on the input) counts as a
failure, and `ok` counts as a success, unless it carried no prediction, in which case it counts
as a failure too. Every other outcome, validation errors, a shed request, a transport failure, a
deadline, an unknown stream, an unexpected error, never touches drift. Telemetry (the embedding
stats and the input-drift baseline feed) is offered only for `ok`.

### Delivery semantics

**HTTP push is synchronous.** The caller gets the answer in the response. Send an
`Idempotency-Key` header (1-200 visible ASCII characters) to make a retried push safe: a replay
of a key already seen within 600 seconds is served again but not counted a second time, and the
answer carries `Idempotent-Replayed: true`.

**Kafka is at least once.** An offset is stored only once its message reaches a terminal result:
answered (the reply produced and its delivery confirmed, when `reply_topic` is set) or
dead-lettered (the database record written, plus its `dlq_topic` copy and failure reply when
those are configured). Concretely, an offset is stored only after the reply has been delivered
*and* the telemetry for it has been queued: nothing is skipped by a crash between the two. A
message that keeps failing past `limits.max_attempts` is dead-lettered as `retries_exhausted`. A
message parked for a retry that then falls out of the log before it can be re-fetched (retention,
compaction, an explicit delete) is dead-lettered as `expired_from_log` instead, and never
silently dropped.

What an HTTP caller sees, by outcome:

| Outcome | Status | Meaning |
|---|---|---|
| `ok` | 200 | served; the body carries the prediction |
| `validation` | 422 | the message is not valid for this stream (bad JSON, a bad envelope, or a schema mismatch) |
| oversize body | 413 | larger than the stream's (or the service's) byte cap |
| unknown stream, not a push stream, or model/alias not served | 404 | (a caller without access to the stream gets 403 instead, never this) |
| stream paused | 503, `Retry-After: 30` | an operator paused it; try again later |
| service draining | 503, `Retry-After: 5`, **or a connection error** | the process is shutting down; retry against another replica — see [Shutdown](#shutdown-and-what-a-caller-sees) |
| local shed (in-flight or rate limit) | 429, `Retry-After` | this stream's own admission control, not the model service |
| upstream overloaded | 503, `Retry-After` | the model service itself is over capacity |
| deadline | 504 | the inference budget ran out |
| transport | 502 | the model service could not be reached or answered |
| model | 500 | the model itself failed on this input (not the caller's fault) |
| unexpected | 500 | the platform surprised itself; treat it as a bug report |

### Running it

`EXAMLOPS_DATAPLANE_ROLE` decides which parts of the dataplane service one process runs: `all`
(default) runs everything; `api` mounts the source, pull and stream routes, including push, but
runs no stream connectors; `streams` runs only the connectors (Kafka today) and serves
`/health`, `/ready` and `/metrics`. A single-host deployment runs `all`; a larger one splits push
onto `api` replicas and the long-running connectors onto `streams` replicas.

A connector marked `singleton: true`, one that does not already balance itself across replicas, is
leader-elected: at most one replica runs it at a time, under a lease with a fencing token, so a
stalled or slow replica can never overlap with the one that took over. Kafka does not need this
(`singleton: false`) because its consumer group already balances partitions across however many
replicas run `streams`; the mechanism exists for a future connector, such as a Dataplane bus req/res
one, that has no such group of its own.

#### Shutdown, and what a caller sees

Shutdown is a graceful drain, bounded by `EXAMLOPS_DATAPLANE_DRAIN_SECONDS` (default 20): the
process stops taking new work, lets in-flight pushes and Kafka commits finish, flushes drift and
telemetry, then releases any leader leases it holds. Keep this below the orchestrator's own stop
grace period, or the process is killed mid-flush. The drain logs one line when it begins and one
when it ends, with how long it took and whether anything was still in flight.

A request already being served is answered normally — verified live: a push held mid-body across
`SIGTERM` still got its `200`, three seconds later, and its drift and input snapshots were written
during the drain.

A request that arrives *after* `SIGTERM` is a different matter. The push route and `/ready` do
answer `503` (`Retry-After: 5`, and `{"status": "draining"}`) while draining, but on `SIGTERM`
uvicorn closes its listener and drops idle keep-alive connections first, so in practice a new
caller gets a **connection reset**, not a `503`. Both were measured; the 503 is what a caller sees
when the drain flag is set without the server shutting down — a readiness probe that runs before
`SIGTERM`.

If you want callers to see `503` and `Retry-After` rather than a reset, flip readiness *before*
the signal: a Kubernetes `preStop` hook (or taking the replica out of the load balancer) followed
by a pause of at least one readiness period, so traffic has already moved before the process is
told to stop. Nothing in the service can do this for you — by the time it has the signal, the
listener is already going.

Two tokens govern access: `DATAPLANE_TOKEN` (read, write and ingest) and the optional
`DATAPLANE_INGEST_TOKEN`, which can only push messages and nothing else. Issue the ingest token
to a producer that should never be able to read the catalog, pause a stream or replay a dead
letter. Both need identity federation or a real secret to do anything beyond an open, loopback-
only read; see [Security](#security) above for the exact rules, which apply to streams unchanged.

### Operating it

`POST /streams/{name}/state` sets a binding to `enabled`, `paused` or `disabled` (the `write`
scope). What pausing does depends on the connector: a Kafka stream's assignment is paused in
place, so its consumer keeps polling, keeps its group membership, and keeps a leader lease if it
holds one, resuming exactly where it left off; a connector with no such seam is stopped outright
and started fresh once re-enabled. An HTTP push stream has no running connector to pause: the
push route answers 503 while paused. Disabling a stream stops it the same way disabling a pack
entry does, and a push to it then answers 404, exactly as if the stream did not exist.

**How quickly a state change takes effect.** The push route reads the catalog through a cache
refreshed every `10 s`, not on every request. The replica that served the state change invalidates
its own entry at once, so *that* replica applies the pause on the very next request. Every other
replica converges within its own cache TTL — up to ten seconds, during which it still serves the
stream. Wait that long before concluding a pause did not take, and do not treat pausing as a way
to stop traffic instantly across a fleet; stop the producer for that.

Dead letters live in the database, one row per message a stream could not deliver:

```bash
curl "http://localhost:18010/streams/jpcp-live/dead-letters?limit=20" \
    -H "Authorization: Bearer $DATAPLANE_TOKEN"

curl -X POST "http://localhost:18010/streams/jpcp-live/dead-letters/42/replay" \
    -H "Authorization: Bearer $DATAPLANE_TOKEN"

curl -X DELETE "http://localhost:18010/streams/jpcp-live/dead-letters?older_than=P7D" \
    -H "Authorization: Bearer $DATAPLANE_TOKEN"
```

#### Paging a dead-letter queue

`limit` is clamped to 1…200 (default 50) and `next_cursor` is the `id` of the last row on the
page; pass it back as `cursor` for the next one, and keep going until `next_cursor` is `null`.
`reason` narrows the listing to one failure reason.

Both `reason` and `cursor` are applied by the **query**, not to a page of rows after the fact. That
distinction is the whole contract of the endpoint: a filter applied to an already-limited read can
only ever see the window that read covered, so paging would stop at the store-read ceiling and a
`reason` absent from the newest rows would come back as absent from the stream. `null` therefore
means *this stream has no more dead letters matching your query* — not *none in the newest N* —
and it is safe to page a queue of any size and to ask about a reason that last occurred long ago.

```bash
# every `oversize` dead letter, however far back — page until next_cursor is null
curl "http://localhost:18010/streams/jpcp-live/dead-letters?reason=oversize&limit=200" \
    -H "Authorization: Bearer $DATAPLANE_TOKEN"
```

Listing and fetching one dead letter never returns its payload by default; a payload is stored at
all only when the binding opts in (`options.dlq_store_payload`), and even then only up to 256 KiB,
only when it is valid UTF-8, and only after secrets and personal data are redacted. Reading a
stored payload needs the `write` scope and `include_payload=true` together, and is itself audited
(a read of a payload is a read of message content, not a routine list). Replaying a dead letter
re-offers its stored message to the stream's own ingress; a dead letter with no stored payload
cannot be replayed. A dead letter's retention is `EXAMLOPS_DATAPLANE_DLQ_RETENTION_DAYS` (default
7 days); the dataplane service's own scheduler prunes older rows automatically, and
`DELETE /streams/{name}/dead-letters?older_than=` purges one stream's dead letters on demand.

### Metrics

Every series is prefixed `dataplane_stream_` and carries `project`/`stream` labels unless noted:

- **`requests_total`**: requests by outcome, also labelled `connector` and `model`. A message the
  stream *refused* before any inference — an oversize body (413), a malformed envelope, a bad
  `Idempotency-Key` — is counted too, with outcome `validation`: a stream rejecting everything it
  is sent must not read as a stream nobody uses.
- **`request_duration_seconds`**: admission to reply, a histogram — refusals included, on purpose.
  A 413 is a reply, and it is a fast one, so a stream being flooded with oversize bodies really
  does answer quickly; read its percentiles next to `requests_total` by outcome, or a drop looks
  like an improvement.
- **`in_flight`**: requests currently holding an in-flight permit.
- **`shed_total`**: requests refused by this stream's own admission control, by reason
  (`in_flight` or `rate`).
- **`telemetry_dropped_total`**: telemetry records the spool refused because it was full.
- **`telemetry_failed_total`**: telemetry records the sink failed to persist (no labels: a sink
  failure is process-wide, not a property of one stream).
- **`connector_state`**: one-hot gauge of a stream's current connector state, labelled `state`.
  The whole series disappears when the stream does (deleted from the catalog, or turned into a
  push stream), rather than standing at `stopped` for the life of the process.
- **`messages_expired_total`**: Kafka messages dead-lettered because they left the log while
  parked for a retry.
- **`consumer_lag`**: a Kafka stream's distance from a partition's high watermark, labelled
  `partition`, and cleared when the partition is revoked. The watermark is the broker's cached one,
  refreshed by a real query every `10 s` (`EXAMLOPS_DATAPLANE_LAG_REFRESH_SECONDS`) — without that
  query a partition nobody is fetching, because it is paused or parked in a retry backoff, would
  report no lag at all, which is precisely when an operator needs the number. A stream that has
  not consumed anything yet still reports: the distance is measured from the group's committed
  offset, or from the log start when the group has committed nothing. **A stream that is
  `paused` in the catalog before it ever starts has no series at all** — it never joins the
  consumer group, so nothing owns its partitions; read a missing series as "not running", never
  as "no backlog".
- **`dead_letters_total`**: dead letters recorded, by reason.
- **`embedding_{norm,mean,std}`** and **`embedding_{norm,mean,std}_baseline`**: the latest
  request's embedding summary and the recorded baseline, labelled `model` only (not
  project/stream).

Alerts and a runbook for these series are not written yet; they land with the rest of the
deployment wiring in a later batch.

### What is not here yet

- No `exa` command for streams: no create, list, pause, dead-letter, or anything else.
- No dashboard page for streams.
- No Dataplane bus req/res connector (the model YAML's `dataplane_bus_uuid` legacy shim is unrelated and
  gated behind its own environment variable).
- No HTTP route to define or edit a stream: `inference.streams` in the model YAML is the only way
  in for now.
- No NATS (or other external) telemetry sink: telemetry is written to `platform.db` only.
- No alerts or runbook for the `dataplane_stream_*` metrics above.

## Plugins

A third-party connector registers under the `exa.dataplane.connectors` entry-point group; it runs
at the same trust tier as any other trusted plugin (see the
[provider security guide](provider-security-trust-tiers.md) for what that tier means). A minimal
one:

```python
# my_connector/plugin.py
from examlops.dataplane.connectors.base import BaseConnector
from examlops.dataplane.types import Probe, TableBatch, Watermark


class MyConnector(BaseConnector):
    kind = "my-source"
    connection_kinds = ("my-source",)
    extra = "my-connector-pkg"  # pip install hint shown when a dependency is missing
    requires = ("my_client_library",)
    required_spec = ("endpoint",)

    def probe(self, conn, spec=None):
        # A reachability check only — never reads or returns data.
        return Probe(True, "reachable")

    def read(self, conn, spec, since, limits):
        import pyarrow as pa

        batch = pa.RecordBatch.from_pylist([{"id": 1, "value": "example"}])
        yield TableBatch("my_table", batch, watermark=None)
```

```toml
# my_connector/pyproject.toml
[project.entry-points."exa.dataplane.connectors"]
my-source = "my_connector.plugin:MyConnector"
```

`exa dataplane connectors` lists it once the package is installed, alongside whether its own
dependencies are present and any load error.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `unknown connector '<kind>'` | Typo, or the connector's extra is not installed | `exa dataplane connectors` shows every known kind and whether it is available |
| `missing <package> — pip install 'examlops[dataplane-...]'` | The connector's dependency is not installed | Install the extra it names (see [Quick Start](quickstart.md)) |
| `s3:// needs a pyarrow build with S3 support` | pyarrow was built without S3 (a source build, or conda's `pyarrow-core`) | Install pyarrow from the PyPI wheels, or conda's full `pyarrow` |
| `Bucket '<name>' not found` on the first pull | The snapshot store's bucket does not exist; the dataplane never creates buckets | Create it (`mc mb`), or point `EXAMLOPS_DATA_BUCKET` at an existing one |
| A source's pull always reports `unchanged` | The watermark never advances, or nothing new exists upstream | `exa dataplane preview <source>` shows what a pull would read right now; `--full` ignores the watermark |
| `EgressDenied` | The target resolved to a non-public or platform-internal address | Add the hostname or CIDR to `EXAMLOPS_DATAPLANE_ALLOWED_HOSTS` if the target is genuinely trusted |
| `spec.url scheme ... resolved to a local filesystem, which is disabled` | A `file://` source with local files not enabled | Set `EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES=1` only where the service has no access to platform state |
| A pinned training run can't find its snapshot | The dataset entry has no `datasets[].dataplane` binding, or the revision was pruned | Check the model YAML; `exa dataplane snapshots <source>` lists what still exists |
| `DataplaneError` mentioning `datasets[].dataplane.tables` | The dataset asked for a logical path with no table mapping | Add the mapping under `datasets[].dataplane.tables` in the model YAML |
| `prune` refuses: "the catalog has no revision rows" | `platform.db` was lost or restored, so revisions training runs used are unknown | `exa dataplane catalog-rebuild`, then prune again; `--force` only if losing that protection is acceptable |
| `prune` refuses: "a pull of … is running" | A pull of the same source holds its lock | Prune again once the pull has finished (`exa dataplane pulls --source <source>`) |
| `SnapshotIntegrityError` when training on a pinned revision | The snapshot's files no longer hash to its revision id — the store was modified after the commit | Treat the store as tampered with; pull a fresh snapshot and investigate who can write to the bucket |
| `a snapshot revision is 'latest' or a full 64-character …` | A short or malformed `--dataset-revision` | Pass the full id from `exa --json dataplane snapshots <source>` |
| The dataplane service returns 503 on every write | `DATAPLANE_TOKEN` is unset, blank, or a placeholder, and no identity federation is configured | Set a real token, or configure `EXAMLOPS_IAM_CONFIG` |
