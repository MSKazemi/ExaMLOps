---
display_name: "JPCP — Joint Power Consumption Predictor"
summary: "Predicts HPC node power consumption from job telemetry using a Random Forest regressor."
use_cases:
  - "Real-time power budgeting for HPC clusters"
  - "Energy-aware job scheduling"
  - "Carbon footprint estimation for HPC workloads"
status: stable
tags:
  - "regression"
  - "power-consumption"
  - "hpc"
  - "random-forest"
  - "seanergys"
maintainers:
  - "SEANERGYS EuroHPC-JU"
last_reviewed: "2025-05-01"
---

# JPCP — Joint Power Consumption Predictor

JPCP is a regression model developed within the SEANERGYS EuroHPC-JU project for predicting the power consumption of HPC nodes based on job-level telemetry. Accurate power forecasts enable operators to enforce power caps, balance load across nodes, and optimise energy usage without requiring costly inline power meters for every job.

## Technical Approach

The model is backed by a `RandomForestRegressor` (scikit-learn) and inherits the full `SeanergysSklearnModel` interface, including training, prediction, save/load via joblib, and feature-importance reporting. An optional sentence-transformer embedding stage (`Embedding.SB`) can project categorical job metadata into a dense vector space before tree-based learning, though the default configuration (`Embedding.NONE`) operates directly on numerical telemetry features. Evaluation uses RMSE, MAPE, and MSE; the multi-stage lifecycle gate is configured with an RMSE threshold.

## Supported Datasets

JPCP is trained and validated on two datasets:

- **PM100Dataset** — the primary benchmark dataset derived from the PM100 HPC monitoring programme, containing per-node power readings alongside job metadata.
- **FDataDataset** — F-Data parquet records collected from a EuroHPC facility, providing FLOP counts, memory bandwidth, precomputed 384-dim embeddings, and timestamped job entries.

Both datasets can be loaded via the Zenodo (default), MinIO, or Dataplane backends by passing the `BACKEND=` parameter at pipeline invocation time.

## Usage

```bash
# Run the training pipeline with dummy data
make pipeline-run-one MODEL=JPCP DATASET=PM100Dataset DUMMY=1

# Run with real Zenodo data
make pipeline-run-one MODEL=JPCP DATASET=PM100Dataset

# Inference via Ray Serve (Production alias)
curl -X POST http://localhost:8001/predict/JPCP \
     -H "Content-Type: application/json" \
     -d '{"features": [[...]]}'
```
