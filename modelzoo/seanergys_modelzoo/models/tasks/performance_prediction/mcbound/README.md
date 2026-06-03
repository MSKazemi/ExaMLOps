---
display_name: "MCBound — Memory-Compute Bound Classifier (Random Forest)"
summary: "Classifies HPC jobs as memory-bound or compute-bound using a Random Forest on precomputed job-script embeddings."
use_cases:
  - "Automatic tagging of HPC jobs as memory-bound or compute-bound"
  - "Informing scheduler placement decisions"
  - "Workload characterisation for capacity planning"
status: experimental
tags:
  - "classification"
  - "performance-prediction"
  - "random-forest"
  - "embeddings"
  - "hpc"
  - "seanergys"
maintainers:
  - "SEANERGYS EuroHPC-JU"
last_reviewed: "2025-05-01"
---

# MCBound — Memory-Compute Bound Classifier (Random Forest)

MCBound is a binary classification model developed within the SEANERGYS EuroHPC-JU project. Like MACK, it predicts whether an HPC job is *memory-bound* or *compute-bound* from job-level telemetry. MCBound serves as an interpretable, ensemble-based alternative to MACK's gradient-boosting approach, using a `RandomForestClassifier` (scikit-learn) that provides built-in feature importance scores and out-of-bag error estimation without requiring a separate validation split.

## Technical Approach

MCBound is backed by a `RandomForestClassifier` and inherits the full `SeanergysSklearnModel` interface. It operates on the same 384-dimensional precomputed job-script embeddings stored in the FData `embedding` column, parsed through a dedicated `embedding_parsing` transform that normalises each vector into a fixed-size `(384,)` float array. Unlike MACK, MCBound does not apply a KMeans quantisation step on FLOP/bandwidth values — the Random Forest's ensemble of decision trees captures the relevant boundaries directly from the embedding features. An optional sentence-transformer re-encoding path (`Embedding.SB`, default `all-MiniLM-L6-v2`) is available for jobs whose scripts were not pre-embedded offline.

## Supported Datasets

MCBound is trained on the **FDataDataset** — F-Data parquet records from a EuroHPC facility. The relevant columns are `embedding` (384-dim float list) and `pclass` (string label: `"memory-bound"` / `"compute-bound"`). Zenodo, MinIO, and Dataplane backends are all supported.

## Usage

```bash
# Run the training pipeline with dummy data
make pipeline-run-one MODEL=MCBound DATASET=FDataDataset DUMMY=1

# Run with real Zenodo data
make pipeline-run-one MODEL=MCBound DATASET=FDataDataset

# Inference via Ray Serve
curl -X POST http://localhost:8001/predict/MCBound \
     -H "Content-Type: application/json" \
     -d '{"features": [[...]]}'
```
