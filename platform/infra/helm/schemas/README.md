# Vendored JSON schemas for custom resources

kubeconform knows the Kubernetes API, not custom resources. The chart renders three CRD kinds,
and CI validates them strictly against these schemas (`.github/workflows/security.yml`), so a
misspelt field fails the build instead of being dropped by the cluster:

| File | Kind | Source |
|---|---|---|
| `spire.spiffe.io/clusterspiffeid_v1alpha1.json` | ClusterSPIFFEID | Generated from SPIRE's `spire-crds` chart 0.6.1, made strict like kubeconform's `openapi2jsonschema` (see its `$comment`) |
| `monitoring.coreos.com/prometheusrule_v1.json` | PrometheusRule | [datreeio/CRDs-catalog](https://github.com/datreeio/CRDs-catalog), `monitoring.coreos.com/prometheusrule_v1.json` |
| `monitoring.coreos.com/servicemonitor_v1.json` | ServiceMonitor | [datreeio/CRDs-catalog](https://github.com/datreeio/CRDs-catalog), `monitoring.coreos.com/servicemonitor_v1.json` |

Vendored rather than fetched, so the gate does not depend on the network. Refresh a schema when
the chart starts using a newer CRD version, and keep the file name kubeconform's
`{Group}/{ResourceKind}_{ResourceAPIVersion}.json` pattern.
