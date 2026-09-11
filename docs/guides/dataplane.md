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
An `http://` endpoint is pinned to the address the check approved (so the request's `Host` is that
IP — a name-based virtual host behind an ingress will not answer it); an `https://` one keeps its
hostname for TLS. This check is weaker than the one on HTTP sources: it covers only the **first
hop**, an `https://` endpoint is not pinned against DNS rebinding, and pyarrow's S3 client follows
redirects to any host. Allow-list only S3 endpoints you trust, and give the dataplane container a
network egress policy. The connection may carry a `region` (`eu-west-1`). A connection with no `access_key`/`secret`
reads anonymously; a source never borrows the service's own AWS credentials. The region is
otherwise `AWS_REGION`, then `AWS_DEFAULT_REGION` (else `us-east-1`, which MinIO ignores), so set
one for an AWS bucket in another region. Buckets are never created — the store's bucket must
already exist: create `EXAMLOPS_DATA_BUCKET` before the first pull.

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
identical snapshot; freshness (below) still resets to zero. Ask the dataplane service to run a
pull instead of running it in-process:

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
  it. S3 endpoints are checked on the first hop only (see *Connect a source*); a network egress
  policy on the dataplane container is the backstop for them.
- **Local files are gated.** A `file://` source/sink is refused unless
  `EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES=1` — the dataplane service's own container should never
  read platform state through a connector.
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

## The service

`platform/services/dataplane` wraps the same library the CLI uses behind an HTTP API, an interval
scheduler that fires each source on its own `--schedule`, and Prometheus metrics — so a pull can
run unattended instead of only from an operator's `exa dataplane pull`.

```bash
DATAPLANE_TOKEN=<a real secret> python platform/services/dataplane/main.py   # listens on :8010
curl http://localhost:8010/health
curl http://localhost:8010/metrics
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
cannot be read at all. `dataplane_source_schedule_seconds` is a scheduled source's pull interval,
published only for an enabled source whose schedule parses — alert when freshness passes twice it.

`exa dataplane pull <source> --remote` is the CLI-side view of the same API — see
[Pull and inspect](#pull-and-inspect) above.

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
