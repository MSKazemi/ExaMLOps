---
display_name: "MACK — Memory-Aware Compute-bound Kernel Classifier"
summary: "Classifies HPC jobs as memory-bound or compute-bound using XGBoost on precomputed job-script embeddings."
use_cases:
  - "Automatic tagging of HPC jobs as memory-bound or compute-bound"
  - "Informing scheduler placement decisions"
  - "Workload characterisation for capacity planning"
status: experimental
tags:
  - "classification"
  - "performance-prediction"
  - "xgboost"
  - "embeddings"
  - "hpc"
  - "seanergys"
maintainers:
  - "SEANERGYS EuroHPC-JU"
last_reviewed: "2025-05-01"
---

# MACK — Memory-Aware Compute-bound Kernel Classifier

MACK is a binary classification model developed within the SEANERGYS EuroHPC-JU project. It predicts whether an HPC job is *memory-bound* or *compute-bound* based on job-level telemetry, enabling schedulers and system administrators to make more informed placement and tuning decisions. Correct classification reduces cache thrashing, improves bandwidth utilisation, and can significantly lower time-to-solution for tightly coupled workloads.

## Technical Approach

MACK is backed by an `XGBClassifier` (XGBoost) and uses two complementary feature representations. First, a KMeans quantisation step (`k=2` centroids each) discretises raw FLOP counts and memory bandwidth values into ordered cluster labels, capturing non-linear workload boundaries without manual binning. Second, precomputed 384-dimensional sentence-transformer embeddings of the job script (`embedding` column in FData parquet) provide a rich semantic signal about the computation being performed. An optional live re-encoding path (`Embedding.SB`) via a configurable `SentenceTransformer` model (default `google/embeddinggemma-300m`) is available for inference on jobs whose scripts were not pre-embedded. The KMeans objects are persisted alongside the XGBoost estimator and reloaded atomically via joblib.

## Supported Datasets

MACK is trained on the **FDataDataset** — F-Data parquet records from a EuroHPC facility. The relevant columns are `embedding` (384-dim float list), `flops` (double), `mbwidth` (memory bandwidth, double), and `pclass` (string label: `"memory-bound"` / `"compute-bound"`). Zenodo, MinIO, and Dataplane backends are all supported.

## Usage

```bash
# Run the training pipeline with dummy data
make pipeline-run-one MODEL=MACK DATASET=FDataDataset DUMMY=1

# Run with real Zenodo data
make pipeline-run-one MODEL=MACK DATASET=FDataDataset

# Inference via Ray Serve
curl -X POST http://localhost:8001/predict/MACK \
     -H "Content-Type: application/json" \
     -d '{"features": [[...]]}'
```
