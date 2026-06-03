---
display_name: "RUAD — Random Unsupervised Anomaly Detection"
summary: "Detects anomalous HPC job behaviour using unsupervised learning on job telemetry."
use_cases:
  - "Detection of abnormal HPC job execution patterns"
  - "Identification of hardware faults or misconfigured jobs"
  - "Proactive alerting before job failure or resource waste"
status: experimental
tags:
  - "anomaly-detection"
  - "classification"
  - "unsupervised"
  - "hpc"
  - "seanergys"
maintainers:
  - "SEANERGYS EuroHPC-JU"
last_reviewed: "2025-05-01"
---

# RUAD — Random Unsupervised Anomaly Detection

RUAD is an anomaly detection model developed within the SEANERGYS EuroHPC-JU project. It targets the identification of HPC jobs whose runtime behaviour deviates significantly from the expected distribution — covering scenarios such as misconfigured job scripts, silent hardware degradation, unexpected memory pressure, or runaway processes that consume resources without producing useful output.

## Technical Approach

RUAD follows an unsupervised approach: no labelled anomaly examples are required during training. The model learns a representation of normal job behaviour from historical telemetry and flags jobs whose characteristics fall outside learned boundaries. The `SeanergysSklearnModel` base class provides the standard fit/predict/save/load contract, and the model task type is `CLASSIFICATION` so that promotion gates and Ray Serve routing treat it consistently with other classifiers in the modelzoo.

## Supported Datasets

RUAD operates on HPC job telemetry exposed through the FData and PM100 dataset interfaces. Both Zenodo (default) and MinIO/Dataplane backends are supported, allowing the model to be retrained on fresh job observations as they arrive from the facility dataplane.

## Usage

```bash
# Run the training pipeline with dummy data
make pipeline-run-one MODEL=RUAD DATASET=FDataDataset DUMMY=1

# Inference via Ray Serve
curl -X POST http://localhost:8001/predict/RUAD \
     -H "Content-Type: application/json" \
     -d '{"features": [[...]]}'
```

> **Note:** RUAD is currently experimental. Threshold tuning for the anomaly score and integration with the multi-stage MLflow lifecycle gate are under active development.
