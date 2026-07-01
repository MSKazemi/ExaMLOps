# ExaMLOps Inference Clients

Command-line clients for interacting with the ExaMLOps Ray Serve inference API.

## Prerequisites

The Ray Serve stack must be running:

```bash
make stack-up          # Docker Compose stack, including Ray Serve on :18001
exa serve check        # verify Ray Serve is reachable
```

`httpx` is the only dependency — it is installed automatically on first run if missing, or via:

```bash
pip install httpx
```

---

## dummy_client.py

General-purpose client for smoke-testing, exploration, and load generation.

### Modes

#### Default — health check + example prediction

Prints service status, lists loaded models, and sends one example request per model.

```bash
python platform/clients/dummy_client.py
# or via Exa CLI:
exa serve check
```

Sample output:

```
── Health ──────────────────────────────────────────────
  status        : ok
  models_loaded : 1
  jpcp                 v2  run_id=9a8e9d2f5c26430a9ea3ea1bebcc2e24

── Models ──────────────────────────────────────────────
  jpcp                 v2  run_id=9a8e9d2f5c26430a9ea3ea1bebcc2e24

── Example predictions ─────────────────────────────────
  POST /predict/jpcp
    prediction: 90.96
    version   : 2  run_id=9a8e9d2f5c26430a9ea3ea1bebcc2e24
    latency   : 61.2 ms
```

#### `--benchmark N` — latency benchmark

Sends N random requests and reports min / p50 / p95 / max latency.

```bash
python platform/clients/dummy_client.py --benchmark 100
# or via Exa CLI (200 requests by default):
exa serve benchmark
```

To generate sustained traffic for Grafana (3 waves × 100 requests):

```bash
for i in 1 2 3; do
  python platform/clients/dummy_client.py --benchmark 100
  sleep 5
done
```

#### `--features JSON` — single custom request

Send a hand-crafted feature dict to a specific model.

```bash
# JPCP expects a 384-dimensional embedding vector:
python platform/clients/dummy_client.py \
  --model jpcp \
  --features '{"embedding": [0.42, 0.17, 0.93, ...]}'  # 384 floats
```

If `--model` is omitted the first loaded model is used.

### CLI reference

| Flag | Default | Description |
|---|---|---|
| `--url` | `http://localhost:18001` | Ray Serve base URL |
| `--model` | first loaded model | Target model name |
| `--features JSON` | — | Custom feature dict (single request) |
| `--benchmark N` | — | Send N random requests, print latency stats |

---

## Model feature schemas

| Model | Input | Notes |
|---|---|---|
| `jpcp` | `{"embedding": [float × 384]}` | FData-trained; 384-dim embedding vector |

When a new model is added to the registry, add its feature schema to `EXAMPLE_FEATURES` and `RANDOM_FEATURE_RANGES` in `dummy_client.py` so `--benchmark` works for it automatically.

---

## Grafana dashboard

After sending traffic, the online metrics dashboard populates at:

```
http://localhost:3000/d/examlops-online-metrics
```

Panels: **Models Loaded**, **Requests/min**, **Latency heatmap**, **Prediction value distribution**, **Error rate**.
