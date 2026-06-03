# ModelZoo Architecture Notes

*Discussion document for the SEANERGYS modelzoo. For internal review with colleagues.*

---

## Context

- **ModelZoo** lives in GitLab as a Python package (Poetry).
- **Serving** uses MLflow (model registry + deployment).
- **MLOps pipeline** coordinates training → register to MLflow → deploy/serve.

---

## 1. Different Models → Different Dependencies

The modelzoo contains many models; each model family may require different frameworks:

| Model type | Typical dependencies |
|------------|----------------------|
| PyTorch models | torch, torchvision |
| TensorFlow models | tensorflow |
| Classical ML | scikit-learn, xgboost |
| NLP / embeddings | sentence-transformers, transformers |

**Implication:** We should **not** force all dependencies on every user. Use **Poetry extras** (optional dependency groups) so consumers install only what they need:

- `poetry add seanergys-modelzoo[torch]` — for PyTorch-based models only
- `poetry add seanergys-modelzoo[sklearn]` — for classical ML
- `poetry add seanergys-modelzoo[all]` — for CI / full zoo

**Open point to discuss:** How to organize extras per model vs per framework?

---

## 2. Serving: MLflow → No Docker Needed

**Serving is handled by MLflow.** We register trained models to MLflow, and MLflow serves them (native serving, or export to SageMaker, Azure ML, etc.).

- **ModelZoo** provides: model code, training logic, configs.
- **MLflow** provides: model registry, versioning, serving endpoints.

**Conclusion: We do NOT need Docker for serving.** MLflow runs the serving layer; we only need to train models and log them to MLflow.

---

## 3. Training & Inference: Different Runners, Different Packaging

| Stage | Runner / Packaging | Docker? |
|-------|--------------------|---------|
| **Training** | MLOps pipeline job | Optional: can use Docker (framework-specific images) or plain Python (venv/poetry) |
| **Inference** | MLflow serving | No — MLflow handles it |
| **Local dev / CI** | Local machine, GitLab CI | Optional — venv + Makefile may be enough |

**Options for training/inference runners:**

1. **Plain Python** — Install modelzoo via `pip`/`poetry` from GitLab; run training script. No containers.
2. **Docker** — Use Docker images (e.g. per framework: `modelzoo-torch`, `modelzoo-sklearn`) for reproducible training in MLOps. Useful when runners are container-based (Kubernetes, etc.).
3. **Hybrid** — Python for simple setups; Docker when the MLOps pipeline requires containerized jobs.

**Open point to discuss:** Do our MLOps runners expect Docker images, or can they run plain Python jobs?

---

## 4. Summary for Colleagues

| Topic | Decision / open point |
|-------|------------------------|
| **Serving** | MLflow — no Docker needed ✅ |
| **Dependencies** | Poetry extras per framework — open: exact grouping |
| **Training** | Different runners possible — Docker optional, plain Python OK |
| **Inference** | Via MLflow — no Docker from modelzoo side |
| **Docker in modelzoo** | Only if we want framework-specific images for training jobs; not required for serving |

---

## Questions to Resolve

1. How are MLOps training jobs run today — container-based or plain Python?
2. Do we need framework-specific Docker images, or is `pip install` from GitLab sufficient?
3. How should we structure Poetry extras: per model, per framework, or both?

---

*Last updated: 2025-03-13*
