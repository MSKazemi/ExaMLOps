# OpenLineage schemas (vendored for offline tests)

Copies of the published OpenLineage JSON Schemas, used by
`tests/unit/test_lineage_openlineage_conformance.py` to check that the events ExaMLOps emits
validate. Vendored so the test needs no network.

| File | Source |
|---|---|
| `OpenLineage-2-0-2.json` | https://openlineage.io/spec/2-0-2/OpenLineage.json |
| `DatasetVersionDatasetFacet-1-0-1.json` | https://openlineage.io/spec/facets/1-0-1/DatasetVersionDatasetFacet.json |

Both files are part of the OpenLineage project (https://github.com/OpenLineage/OpenLineage) and
are licensed under the Apache License 2.0. They are copied unmodified.
