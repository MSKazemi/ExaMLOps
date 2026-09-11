"""The dataplane (ADR 0130): connectors -> versioned snapshots -> pinned training data."""

from examlops.dataplane.pull import (
    PullResult,
    SourceDef,
    define_source,
    get_source_def,
    list_source_defs,
    preview,
    remove_source,
    run_pull,
)
from examlops.dataplane.pull import test_source as probe_source
from examlops.dataplane.store import (
    SnapshotManifest,
    SnapshotRef,
    materialize,
    read_manifest,
    resolve,
    source_key,
    store_from_env,
)

__all__ = [
    "PullResult",
    "SnapshotManifest",
    "SnapshotRef",
    "SourceDef",
    "define_source",
    "get_source_def",
    "list_source_defs",
    "materialize",
    "preview",
    "probe_source",
    "read_manifest",
    "remove_source",
    "resolve",
    "run_pull",
    "source_key",
    "store_from_env",
]
