# =============================================================================
# ExaMLOps — Makefile
# Run all targets from the repository root.
# =============================================================================

# ── Paths ─────────────────────────────────────────────────────────────────────

COMPOSE_DIR     := infra/docker-compose
RAY_SERVING_DIR := services/ray_serving
MODELZOO_DIR    := modelzoo
PID_DIR         := .run

# ── Python / uv ───────────────────────────────────────────────────────────────

VENV   := .venv
PYTHON := $(VENV)/bin/python
UV     := uv

# ── ANSI colours (disable by setting NO_COLOR=1) ──────────────────────────────

ifndef NO_COLOR
  BOLD   := \033[1m
  DIM    := \033[2m
  CYAN   := \033[36m
  GREEN  := \033[32m
  YELLOW := \033[33m
  RESET  := \033[0m
else
  BOLD   := ""
  DIM    := ""
  CYAN   := ""
  GREEN  := ""
  YELLOW := ""
  RESET  := ""
endif

# ── Default goal ──────────────────────────────────────────────────────────────

.DEFAULT_GOAL := help

# ── Phony declarations ────────────────────────────────────────────────────────

.PHONY: help \
        dev-up dev-down dev-wipe dev-restart dev-logs \
        postgres-stop mlflow-stop orchestrator-stop \
        ray-serving-start ray-serving-stop \
        monitoring-up monitoring-down \
        venv install install-dev clean \
        pipeline-list pipeline-run pipeline-run-full \
        modelzoo-test \
        lint lint-fix typecheck test test-unit test-integration test-cov check \
        bootstrap status start-all stop-all

# =============================================================================
# HELP — auto-generated from ## and ##@ markers
# =============================================================================

help:
	@awk ' \
	  BEGIN { \
	    FS = ":.*##"; \
	    printf "\n$(BOLD)ExaMLOps$(RESET)  –  MLOps platform for HPC power prediction\n"; \
	    printf "$(DIM)Run all targets from the repo root: make <target>$(RESET)\n\n"; \
	  } \
	  /^##@/ { printf "\n$(BOLD)%s$(RESET)\n", substr($$0, 5) } \
	  /^[a-zA-Z_-]+:.*?##/ { printf "  $(CYAN)%-24s$(RESET) %s\n", $$1, $$2 } \
	' $(MAKEFILE_LIST)
	@printf "\n$(BOLD)Key environment variables:$(RESET)\n"
	@printf "  $(YELLOW)%-36s$(RESET) %s\n" \
	  "MLFLOW_TRACKING_URI"  "MLflow server (default: http://localhost:5000)"
	@printf "\n"

# =============================================================================
##@ Infrastructure
# =============================================================================

dev-up: ## Start full dev stack: Postgres · MLflow · Prefect · Ray Serving
	@cd $(COMPOSE_DIR) && docker compose up -d --build
	@printf "\n$(GREEN)Dev stack is up:$(RESET)\n"
	@printf "  %-28s %s\n" \
	  "MLflow UI"     "http://localhost:5000" \
	  "Prefect UI"    "http://localhost:4200" \
	  "Ray Serving"   "http://localhost:8001  /docs  /health  /predict/{name}" \
	  "Ray Dashboard" "http://localhost:8265"
	@printf "\n"

dev-down: ## Stop and remove all containers (volumes preserved)
	@cd $(COMPOSE_DIR) && docker compose down
	@printf "$(DIM)Dev stack stopped. Volumes preserved — run 'make dev-wipe' to also delete data.$(RESET)\n"

dev-wipe: ## DESTRUCTIVE: remove all containers, volumes, and built images
	@printf "$(YELLOW)$(BOLD)Wiping all ExaMLOps Docker resources...$(RESET)\n"
	@cd $(COMPOSE_DIR) && docker compose --profile monitoring down -v --rmi local 2>/dev/null || true
	@printf "$(GREEN)Done. Run 'make dev-up' to start fresh.$(RESET)\n"

dev-restart: ## Restart all containers without rebuilding images
	@cd $(COMPOSE_DIR) && docker compose restart
	@printf "$(GREEN)Dev stack restarted.$(RESET)\n"

dev-logs: ## Tail live logs from all running containers (Ctrl+C to exit)
	@cd $(COMPOSE_DIR) && docker compose logs -f

# =============================================================================
##@ Individual Services
# =============================================================================

postgres-stop: ## Stop the Postgres container
	@cd $(COMPOSE_DIR) && docker compose stop postgres
	@printf "$(DIM)postgres stopped.$(RESET)\n"

mlflow-stop: ## Stop the MLflow tracking server container
	@cd $(COMPOSE_DIR) && docker compose stop mlflow
	@printf "$(DIM)mlflow stopped.$(RESET)\n"

orchestrator-stop: ## Stop the Prefect orchestration server container
	@cd $(COMPOSE_DIR) && docker compose stop orchestrator
	@printf "$(DIM)orchestrator stopped.$(RESET)\n"

ray-serving-start: install ## Start Ray Serve locally on port 8001 (foreground, Ctrl+C to stop)
	@printf "$(BOLD)Ray Serving$(RESET)  →  http://localhost:8001\n"
	@printf "$(DIM)Docs: http://localhost:8001/docs   Dashboard: http://localhost:8265$(RESET)\n\n"
	@cd $(RAY_SERVING_DIR) && ../../$(PYTHON) app.py

ray-serving-stop: ## Stop the Ray Serve container
	@cd $(COMPOSE_DIR) && docker compose stop ray-serving
	@printf "$(DIM)ray-serving stopped.$(RESET)\n"

# =============================================================================
##@ Monitoring  (optional — Prometheus + Grafana)
# =============================================================================

monitoring-up: ## Start Prometheus (9090) and Grafana (3000)
	@printf "$(BOLD)Starting monitoring stack...$(RESET)\n"
	@cd $(COMPOSE_DIR) && docker compose --profile monitoring up -d prometheus grafana
	@printf "\n$(GREEN)Monitoring stack is up:$(RESET)\n"
	@printf "  %-28s %s\n" \
	  "Prometheus" "http://localhost:9090" \
	  "Grafana"    "http://localhost:3000  (admin / admin)"
	@printf "\n"

monitoring-down: ## Stop Prometheus and Grafana
	@cd $(COMPOSE_DIR) && docker compose --profile monitoring stop prometheus grafana
	@cd $(COMPOSE_DIR) && docker compose --profile monitoring rm -f prometheus grafana
	@printf "$(DIM)Monitoring stack stopped.$(RESET)\n"

# =============================================================================
##@ Training Pipeline
# =============================================================================

pipeline-list: install ## List all registered models and their supported datasets
	@$(PYTHON) pipelines/pipeline_generator.py --list

pipeline-run: install ## Run pipeline for all models (dummy data — safe for development)
	@printf "$(BOLD)Running pipeline (dummy mode)...$(RESET)\n"
	@PREFECT_API_URL=http://localhost:4200/api \
	 MLFLOW_TRACKING_URI=http://localhost:5000 \
	 $(PYTHON) pipelines/pipeline_generator.py --dummy

pipeline-run-full: install ## Run pipeline with full Zenodo data (production use only)
	@printf "$(BOLD)Running pipeline (full data from Zenodo)...$(RESET)\n"
	@PREFECT_API_URL=http://localhost:4200/api \
	 MLFLOW_TRACKING_URI=http://localhost:5000 \
	 $(PYTHON) pipelines/pipeline_generator.py

# =============================================================================
##@ ModelZoo
# =============================================================================

modelzoo-test: ## Run the upstream modelzoo test suite (smoke + unit)
	@printf "$(BOLD)Running modelzoo tests...$(RESET)\n"
	@cd $(MODELZOO_DIR) && pip install -q poetry && poetry install --with ci -q 2>/dev/null || pip install -q -r requirements.txt
	@cd $(MODELZOO_DIR) && python -m pytest tests/smoke/ tests/unit/ -v --tb=short
	@printf "$(GREEN)ModelZoo tests passed.$(RESET)\n"

# =============================================================================
##@ Python Environment
# =============================================================================

venv: ## Create .venv with uv (no-op if it already exists)
	@test -d $(VENV) || $(UV) venv $(VENV)
	@printf "$(GREEN)$(VENV) ready$(RESET)  (Python: $$($(PYTHON) --version))\n"

install: venv ## Install runtime dependencies into .venv
	@$(UV) pip install -e . -q
	@printf "$(GREEN)Runtime dependencies installed.$(RESET)\n"

install-dev: venv ## Install runtime + dev dependencies (pytest · ruff · mypy)
	@$(UV) pip install -e ".[dev]" -q
	@printf "$(GREEN)Runtime + dev dependencies installed.$(RESET)\n"

clean: ## Remove .venv, build artefacts, and all cache directories
	@rm -rf $(VENV) src/*.egg-info
	@find . -type d -name __pycache__ -not -path '*/.git/*' -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -not -path '*/.git/*' -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .mypy_cache  -not -path '*/.git/*' -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .ruff_cache  -not -path '*/.git/*' -exec rm -rf {} + 2>/dev/null || true
	@printf "$(GREEN)Clean.$(RESET)\n"

# =============================================================================
##@ Code Quality
# =============================================================================

lint: install-dev ## Run ruff linter across src, tests, pipelines, services
	@printf "$(BOLD)Linting...$(RESET)\n"
	@$(VENV)/bin/ruff check src/ tests/ pipelines/ services/
	@printf "$(GREEN)Lint passed.$(RESET)\n"

lint-fix: install-dev ## Run ruff and auto-fix all safe issues
	@$(VENV)/bin/ruff check --fix src/ tests/ pipelines/ services/

typecheck: install-dev ## Run mypy type checker on pipelines and services
	@printf "$(BOLD)Type checking...$(RESET)\n"
	@$(VENV)/bin/mypy pipelines/ services/ --ignore-missing-imports
	@printf "$(GREEN)Type check passed.$(RESET)\n"

test: install-dev ## Run the full test suite
	@$(VENV)/bin/pytest tests/ -v --tb=short

test-unit: install-dev ## Run unit tests only
	@$(VENV)/bin/pytest tests/unit/ -v --tb=short

test-integration: install-dev ## Run integration tests only
	@$(VENV)/bin/pytest tests/integration/ -v --tb=short

test-cov: install-dev ## Run tests with HTML coverage report → htmlcov/index.html
	@$(VENV)/bin/pytest tests/ --cov=pipelines --cov-report=html --cov-report=term-missing
	@printf "$(GREEN)Coverage report: htmlcov/index.html$(RESET)\n"

check: lint typecheck test ## Run all quality checks: lint · typecheck · test
	@printf "\n$(GREEN)$(BOLD)All checks passed.$(RESET)\n\n"

# =============================================================================
##@ Convenience
# =============================================================================

bootstrap: dev-up install-dev ## One-shot setup: start dev stack + install all deps
	@printf "\n$(GREEN)$(BOLD)Bootstrap complete.$(RESET)\n\n"
	@printf "  Next steps:\n"
	@printf "    make pipeline-list        # see registered models\n"
	@printf "    make pipeline-run         # run pipeline (dummy data, no download)\n"
	@printf "    make pipeline-run-full    # run pipeline (full Zenodo data)\n"
	@printf "    make check                # run all quality checks\n\n"

status: ## Show running containers, endpoints, and venv state
	@printf "$(BOLD)Docker containers:$(RESET)\n"
	@cd $(COMPOSE_DIR) && docker compose ps 2>/dev/null || printf "  $(DIM)(docker compose not running)$(RESET)\n"
	@printf "\n$(BOLD)Endpoints:$(RESET)\n"
	@printf "  %-28s %s\n" \
	  "MLflow UI"      "http://localhost:5000" \
	  "Prefect UI"     "http://localhost:4200" \
	  "Ray Serving"    "http://localhost:8001  /docs  /health  /models  /predict/{name}" \
	  "Ray Dashboard"  "http://localhost:8265" \
	  "Prometheus"     "http://localhost:9090  (if monitoring-up)" \
	  "Grafana"        "http://localhost:3000  (if monitoring-up)"
	@printf "\n$(BOLD)Python environment:$(RESET)\n"
	@test -d $(VENV) \
	  && printf "  $(GREEN)%-28s$(RESET) %s\n" "$(VENV)" "$$($(PYTHON) --version 2>&1)" \
	  || printf "  $(YELLOW)$(VENV) not found$(RESET) — run: make venv\n"
	@printf "\n"

start-all: install ## Start Docker stack + Ray Serving in the background
	@mkdir -p $(PID_DIR)
	@cd $(COMPOSE_DIR) && docker compose up -d --build
	@printf "Starting Ray Serving in background...\n"
	@cd $(RAY_SERVING_DIR) && ../../$(PYTHON) app.py \
	    > ../../$(PID_DIR)/ray-serving.log 2>&1 & echo $$! > $(PID_DIR)/ray-serving.pid
	@printf "\n$(GREEN)$(BOLD)All services started.$(RESET)\n\n"
	@printf "  %-28s %s\n" \
	  "MLflow UI"     "http://localhost:5000" \
	  "Prefect UI"    "http://localhost:4200" \
	  "Ray Serving"   "http://localhost:8001" \
	  "Ray Dashboard" "http://localhost:8265"
	@printf "\n  $(DIM)Logs: $(PID_DIR)/ray-serving.log$(RESET)\n"
	@printf "  $(DIM)Stop with: make stop-all$(RESET)\n\n"

stop-all: ## Stop all background processes + all Docker containers
	@printf "$(BOLD)Stopping all services...$(RESET)\n"
	@for pidfile in $(PID_DIR)/ray-serving.pid; do \
	    if [ -f "$$pidfile" ]; then \
	        kill $$(cat "$$pidfile") 2>/dev/null && printf "  killed $$pidfile\n" || true; \
	        rm -f "$$pidfile"; \
	    fi; \
	done
	@cd $(COMPOSE_DIR) && docker compose --profile monitoring stop prometheus grafana 2>/dev/null || true
	@cd $(COMPOSE_DIR) && docker compose down
	@printf "\n$(GREEN)All services stopped.$(RESET)\n"
