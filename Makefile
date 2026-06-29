# =============================================================================
# ExaMLOps — Production Makefile
# Run all targets from the repository root.
# Requires: uv ≥ 0.4  ·  Python 3.12+  ·  Docker with Compose v2
# =============================================================================



SHELL := /bin/bash -euo pipefail

# ── Versions ──────────────────────────────────────────────────────────────────
PYTHON_MIN := 3.12

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT_DIR        := $(CURDIR)
COMPOSE_DIR     := platform/infra/docker-compose
RAY_SERVING_DIR := serving/ray_serving
MODELZOO_DIR    := modelzoo
# lxp layout: /nfs/share01/examlops-seanerbus/seanerbus
# laptop layout: ../../seanerbus  (two levels up from repo root)
SEANERBUS_DIR   ?= $(or $(wildcard $(ROOT_DIR)/../examlops-seanerbus/seanerbus),$(ROOT_DIR)/../../seanerbus)
PID_DIR         := .run

# ── Docker Compose  ───────────────────────────────────────────────────────────
# .env lives at the repo root; compose runs from COMPOSE_DIR so we must pass
# --env-file explicitly (docker compose only auto-loads .env from CWD).
DC := docker compose --env-file $(ROOT_DIR)/.env

# ── Python / uv ───────────────────────────────────────────────────────────────
VENV   := .venv
PYTHON := $(VENV)/bin/python
UV     := uv

# ── MinIO / S3 artifact store ─────────────────────────────────────────────────
# Dev defaults match docker-compose.yml.  Override in shell or .env for production:
#   export MINIO_ENDPOINT=https://minio.your-cluster MINIO_ACCESS_KEY=... MINIO_SECRET_KEY=...
MINIO_ENDPOINT   ?= http://localhost:19000
MINIO_ACCESS_KEY ?= minioadmin
MINIO_SECRET_KEY ?= minioadmin

# ── ANSI colours (disable with NO_COLOR=1) ────────────────────────────────────
ifdef NO_COLOR
  BOLD   :=
  DIM    :=
  RED    :=
  GREEN  :=
  YELLOW :=
  CYAN   :=
  RESET  :=
else
  BOLD   := \033[1m
  DIM    := \033[2m
  RED    := \033[31m
  GREEN  := \033[32m
  YELLOW := \033[33m
  CYAN   := \033[36m
  RESET  := \033[0m
endif

.DEFAULT_GOAL := help

.PHONY: help \
        full-up stop-all rebuild rebuild-all lxp-rebuild \
        stack-up stack-down stack-wipe stack-restart stack-logs stack-ps stack-shell \
        touch-env-dashboard \
        monitoring-up monitoring-down \
        seanerbus-up seanerbus-down seanerbus-bridge-logs seanerbus-reqgen-logs \
        seanerbus-install seanerbus-bridge-up seanerbus-test-req \
        dashboard-up dashboard-logs dashboard-check \
        jupyter-up jupyter-down jupyter-logs jupyter-add-user \
        control-plane-up control-plane-down control-plane-logs \
        firewall-fix-up firewall-fix-down firewall-fix-logs \
        agent \
        venv install install-dev clean \
        lint lint-fix typecheck test test-unit test-integration test-cov check \
        ci ci-modelzoo ci-infra ci-examlops \
        modelzoo-test agent-test \
        docs-serve docs-build \
        bootstrap \
        _guard-uv _guard-python _guard-service

# =============================================================================
##@ Help
# =============================================================================

help: ## Show this help message
	@awk ' \
	  BEGIN { \
	    FS = ":.*##"; \
	    printf "\n$(BOLD)ExaMLOps$(RESET)  —  General-purpose MLOps platform with auto-discovery pipelines\n"; \
	    printf "$(DIM)Run targets from the repo root: make <target>$(RESET)\n\n"; \
	  } \
	  /^##@/ { printf "\n$(BOLD)%s$(RESET)\n", substr($$0, 5) } \
	  /^[a-zA-Z_-]+:.*?##/ { printf "  $(CYAN)%-28s$(RESET) %s\n", $$1, $$2 } \
	' $(MAKEFILE_LIST)
	@printf "\n$(BOLD)Service endpoints$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "MLflow UI"        "http://localhost:15000" \
	  "Prefect UI"       "http://localhost:14200" \
	  "Ray Serve API"    "http://localhost:18001  (/docs · /health · /models)" \
	  "Ray Dashboard"    "http://localhost:18265" \
	  "MinIO Console"    "http://localhost:19001  (minioadmin / minioadmin)" \
	  "MinIO S3 API"     "http://localhost:19000" \
	  "Prometheus"       "http://localhost:19090  (monitoring profile)" \
	  "Grafana"          "http://localhost:13000  (monitoring profile)" \
	  "Dashboard"        "http://localhost:18099  (ExaMLOps central dashboard)" \
	  "JupyterHub"       "http://localhost:18888  (jupyter profile — make jupyter-up)"
	@printf "\n$(BOLD)Override variables$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "SERVICE=<name>"  "container for stack-shell    (e.g. mlflow)" \
	  "NO_COLOR=1"      "disable colour output (for CI logs)"
	@printf "\n$(BOLD)Examples$(RESET)\n"
	@printf "  $(DIM)# First-time setup$(RESET)\n"
	@printf "  $(CYAN)make bootstrap$(RESET)\n\n"
	@printf "  $(DIM)# Day-to-day MLOps operations (training, deployment, approvals, serving)$(RESET)\n"
	@printf "  $(CYAN)exa pipeline run --model JPCP --dataset PM100Dataset --dummy$(RESET)\n"
	@printf "  $(CYAN)exa pipeline deploy --no-schedule$(RESET)\n"
	@printf "  $(CYAN)exa serve reload$(RESET)\n\n"
	@printf "  $(DIM)# Start optional monitoring stack$(RESET)\n"
	@printf "  $(CYAN)make monitoring-up$(RESET)\n\n"
	@printf "  $(DIM)# Run all quality checks before committing$(RESET)\n"
	@printf "  $(CYAN)make check$(RESET)\n\n"
	@printf "  $(DIM)# Open a shell inside a running container$(RESET)\n"
	@printf "  $(CYAN)make stack-shell SERVICE=mlflow$(RESET)\n"
	@printf "\n"

# =============================================================================
##@ Stack  (Docker Compose)
# =============================================================================

full-up: _guard-uv ## Start everything: stack + monitoring + SeanerBUS (reqgen + bridge)
	@if ! docker network ls --format "{{.Name}}" | grep -q "^seanerbus-net$$"; then \
	  printf "$(RED)ERROR: Docker network 'seanerbus-net' not found.$(RESET)\n"; \
	  printf "$(DIM)Run once: docker network create seanerbus-net$(RESET)\n"; \
	  exit 1; \
	fi
	@if [ ! -d "$(SEANERBUS_DIR)" ]; then \
	  printf "$(RED)ERROR: SeanerBUS repo not found at $(SEANERBUS_DIR)$(RESET)\n"; \
	  printf "$(DIM)Clone it: git clone <seanerbus-repo> $(SEANERBUS_DIR)$(RESET)\n"; \
	  exit 1; \
	fi
	@printf "$(BOLD)Starting ExaMLOps stack + monitoring + SeanerBUS...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) up -d --build
	@cd $(COMPOSE_DIR) && $(DC) --profile monitoring up -d prometheus grafana loki promtail alertmanager tempo
	@printf "$(DIM)Starting SeanerBUS reqgen...$(RESET)\n"
	@cd $(SEANERBUS_DIR) && docker compose up -d
	@cd $(COMPOSE_DIR) && $(DC) --profile seanerbus up -d seanerbus-bridge
	@printf "\n$(GREEN)All services are up:$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "Dashboard"          "http://localhost:18099" \
	  "MLflow UI"          "http://localhost:15000" \
	  "Prefect UI"         "http://localhost:14200" \
	  "Ray Serve API"      "http://localhost:18001  (/docs · /health · /models)" \
	  "Ray Dashboard"      "http://localhost:18265" \
	  "MinIO Console"      "http://localhost:19001  (minioadmin / minioadmin)" \
	  "Control Plane"      "http://localhost:18002" \
	  "Prometheus"         "http://localhost:19090" \
	  "Alertmanager"       "http://localhost:19093" \
	  "Grafana"            "http://localhost:13000  (admin / admin)" \
	  "Loki"               "http://localhost:13100" \
	  "Tempo"              "http://localhost:13200" \
	  "SeanerBUS Bridge"   "http://localhost:18003  (/health · /stats · /metrics)"
	@printf "\n$(DIM)Logs: make seanerbus-bridge-logs · make seanerbus-reqgen-logs$(RESET)\n\n"

stack-up: _guard-uv ## Start core stack only: Postgres · MLflow · Prefect · Ray Serve · Dashboard · Control Plane
	@printf "$(BOLD)Starting ExaMLOps core stack...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) up -d --build
	@printf "\n$(GREEN)Core stack is up:$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "Dashboard"     "http://localhost:18099" \
	  "MLflow UI"     "http://localhost:15000" \
	  "Prefect UI"    "http://localhost:14200" \
	  "Ray Serve API" "http://localhost:18001  (/docs · /health · /models)" \
	  "Ray Dashboard" "http://localhost:18265"
	@printf "\n$(DIM)Tip: 'make monitoring-up' to also start Prometheus/Grafana/Loki · 'make full-up' for everything$(RESET)\n\n"

stop-all: ## Stop ALL ExaMLOps containers (core + monitoring + SeanerBUS + Jupyter) — prevents auto-restart on reboot
	@printf "$(BOLD)Stopping all ExaMLOps containers...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) \
	  --profile monitoring --profile seanerbus --profile jupyter \
	  down 2>/dev/null || true
	@if [ -d "$(SEANERBUS_DIR)" ]; then \
	  printf "$(DIM)Stopping SeanerBUS reqgen...$(RESET)\n"; \
	  cd $(SEANERBUS_DIR) && docker compose down 2>/dev/null || true; \
	fi
	@printf "$(GREEN)All containers stopped and removed.$(RESET)\n"
	@printf "$(DIM)Volumes preserved. Containers will NOT restart on reboot.$(RESET)\n"

rebuild: ## Force-rebuild ALL images (--no-cache) + restart core + monitoring  ← use after Dockerfile/dep changes
	@printf "$(BOLD)Stopping all running containers...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) \
	  --profile monitoring --profile jupyter --profile seanerbus \
	  down --remove-orphans 2>/dev/null || true
	@printf "$(BOLD)Rebuilding all images (no cache)...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) --profile monitoring build --no-cache
	@printf "$(BOLD)Starting core stack...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) up -d
	@printf "$(BOLD)Starting monitoring stack...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) --profile monitoring up -d \
	  prometheus grafana loki promtail alertmanager tempo
	@printf "\n$(GREEN)$(BOLD)Rebuild complete — all services running:$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "Dashboard"     "http://localhost:18099" \
	  "MLflow UI"     "http://localhost:15000" \
	  "Prefect UI"    "http://localhost:14200" \
	  "Ray Serve API" "http://localhost:18001" \
	  "Control Plane" "http://localhost:18002" \
	  "Prometheus"    "http://localhost:19090" \
	  "Grafana"       "http://localhost:13000  (admin / admin)" \
	  "Loki"          "http://localhost:13100"
	@printf "\n"

rebuild-all: ## Force-rebuild ALL images including JupyterHub + restart EVERYTHING (core + monitoring + jupyter)
	@printf "$(BOLD)Stopping all running containers...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) \
	  --profile monitoring --profile jupyter --profile seanerbus \
	  down --remove-orphans 2>/dev/null || true
	@printf "$(BOLD)Rebuilding JupyterLab user image...$(RESET)\n"
	@docker build --network=host -t examlops-jupyterlab \
	  -f $(COMPOSE_DIR)/Dockerfile.jupyterlab $(COMPOSE_DIR) --no-cache
	@printf "$(BOLD)Rebuilding all service images (no cache)...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) --profile monitoring --profile jupyter build --no-cache
	@printf "$(BOLD)Starting everything...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) up -d
	@cd $(COMPOSE_DIR) && $(DC) --profile monitoring up -d \
	  prometheus grafana loki promtail alertmanager tempo
	@cd $(COMPOSE_DIR) && $(DC) --profile jupyter up -d jupyterhub
	@printf "\n$(GREEN)$(BOLD)Full rebuild complete — all services running:$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "Dashboard"     "http://localhost:18099" \
	  "MLflow UI"     "http://localhost:15000" \
	  "Prefect UI"    "http://localhost:14200" \
	  "Ray Serve API" "http://localhost:18001" \
	  "Control Plane" "http://localhost:18002" \
	  "Prometheus"    "http://localhost:19090" \
	  "Grafana"       "http://localhost:13000  (admin / admin)" \
	  "Loki"          "http://localhost:13100" \
	  "JupyterHub"    "http://localhost:18888"
	@printf "\n"

lxp-rebuild: ## Pull latest code + force-rebuild + restart all containers on lxp-cpu01
	@printf "$(BOLD)Rebuilding on lxp-cpu01...$(RESET)\n"
	@ssh lxp-cpu01 "set -e; \
	  cd /nfs/share01/examlops; \
	  echo '=== git pull ==='; \
	  git pull; \
	  cd platform/infra/docker-compose; \
	  echo '=== stopping all containers ==='; \
	  docker compose --env-file /nfs/share01/examlops/.env \
	    --profile monitoring --profile jupyter --profile seanerbus \
	    down --remove-orphans 2>/dev/null || true; \
	  echo '=== rebuilding all images (no cache) ==='; \
	  docker compose --env-file /nfs/share01/examlops/.env \
	    --profile monitoring build --no-cache; \
	  echo '=== starting core stack ==='; \
	  docker compose --env-file /nfs/share01/examlops/.env up -d; \
	  echo '=== starting monitoring stack ==='; \
	  docker compose --env-file /nfs/share01/examlops/.env \
	    --profile monitoring up -d prometheus grafana loki promtail alertmanager tempo; \
	  echo '=== done ==='; \
	  docker compose --env-file /nfs/share01/examlops/.env ps"
	@printf "\n$(GREEN)$(BOLD)lxp-cpu01 rebuild complete.$(RESET)\n"
	@printf "$(DIM)Connect:  ssh lxp  then open http://localhost:18099$(RESET)\n\n"

stack-down: ## Stop containers — data volumes preserved
	@cd $(COMPOSE_DIR) && $(DC) down
	@printf "$(DIM)Stack stopped. Volumes intact — 'make stack-wipe' to also delete data.$(RESET)\n"

stack-wipe: ## DESTRUCTIVE: remove all containers, volumes, and built images
	@printf "$(RED)$(BOLD)WARNING: this deletes ALL containers, volumes, and MLflow data.$(RESET)\n"
	@printf "$(YELLOW)Type 'yes' to confirm: $(RESET)" ; read -r _confirm ; \
	  [ "$$_confirm" = "yes" ] || (printf "$(DIM)Aborted.$(RESET)\n" ; exit 1)
	@cd $(COMPOSE_DIR) && $(DC) --profile monitoring down -v --rmi local 2>/dev/null || true
	@printf "$(GREEN)Stack wiped. Run 'make stack-up' to start fresh.$(RESET)\n"

stack-restart: ## Restart all containers without rebuilding images
	@cd $(COMPOSE_DIR) && $(DC) restart
	@printf "$(GREEN)Stack restarted.$(RESET)\n"

stack-logs: ## Tail live logs from all containers (Ctrl+C to stop)
	@cd $(COMPOSE_DIR) && $(DC) logs -f

stack-ps: ## Show current container status
	@cd $(COMPOSE_DIR) && $(DC) ps 2>/dev/null \
	  || printf "  $(DIM)(stack not running — run 'make stack-up')$(RESET)\n"

stack-shell: _guard-service ## Open a bash shell inside a container  SERVICE=<name>
	@docker exec -it examlops-$(SERVICE) bash

touch-env-dashboard: ## Ensure .env.dashboard exists (avoids compose error on first run)
	touch .env.dashboard

# =============================================================================
##@ Monitoring  (Prometheus + Grafana — optional)
# =============================================================================

monitoring-up: ## Start Prometheus (9090) · Alertmanager (9093) · Tempo (3200) · Grafana (3000) · Loki (3100) · Promtail
	@printf "$(BOLD)Starting monitoring stack...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) --profile monitoring up -d prometheus grafana loki promtail alertmanager tempo
	@printf "\n$(GREEN)Monitoring is up:$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "Prometheus"    "http://localhost:19090" \
	  "Alertmanager"  "http://localhost:19093  (alert routing + silence management)" \
	  "Tempo"         "http://localhost:13200  (traces — use Grafana Explore to view)" \
	  "Grafana"       "http://localhost:13000  (admin / admin)" \
	  "Loki"          "http://localhost:13100  (datasource auto-provisioned in Grafana)"
	@printf "\n"

# =============================================================================
##@ SeanerBUS Bridge  (Docker Compose container)
# =============================================================================
# Prerequisite: real SeanerBUS must be running on port 5398.
#   cd ../seanerbus && docker compose up -d
#
# Default: bridge connects to host.docker.internal:5398 in reqres mode.
# Override host:  SEANERBUS_HOST=<ip> make seanerbus-up

seanerbus-up: ## Start ExaMLOps SeanerBUS bridge container (NOT the SeanerBUS system itself)
	@printf "$(BOLD)Starting SeanerBUS bridge...$(RESET)\n"
	@printf "$(DIM)Note: this starts the bridge inside ExaMLOps, not the SeanerBUS system.$(RESET)\n"
	@printf "$(DIM)      To start SeanerBUS first: cd $(SEANERBUS_DIR)/.. && docker compose up -d$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) --profile seanerbus up -d seanerbus-bridge
	@printf "$(GREEN)SeanerBUS bridge is up:$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "Bridge endpoint" "http://localhost:18003  (/health · /stats)" \
	  "Logs" "make seanerbus-bridge-logs"
	@printf "\n"

seanerbus-down: ## Stop ExaMLOps SeanerBUS bridge container (does NOT stop the SeanerBUS system)
	@cd $(COMPOSE_DIR) && $(DC) --profile seanerbus stop seanerbus-bridge
	@cd $(COMPOSE_DIR) && $(DC) --profile seanerbus rm -f seanerbus-bridge
	@printf "$(DIM)SeanerBUS bridge stopped. SeanerBUS system is unaffected.$(RESET)\n"

seanerbus-bridge-logs: ## Tail SeanerBUS bridge logs
	@cd $(COMPOSE_DIR) && $(DC) --profile seanerbus logs -f seanerbus-bridge

seanerbus-reqgen-logs: ## Tail SeanerBUS request generator logs (inference_requests.log + seanerbus.log)
	@tail -f $(SEANERBUS_DIR)/logs/inference_requests.log $(SEANERBUS_DIR)/logs/seanerbus.log


# =============================================================================
##@ SeanerBUS Integration  (Python CLI tools)
# =============================================================================

SEANERBUS_SERVER ?= localhost
SEANERBUS_PORT   ?= 5398

seanerbus-install: ## Install seanerbus Python bindings + pycapnp into .venv
	uv pip install -e ".[seanerbus]"

seanerbus-bridge-up: ## Start seanerbus bridge bare-metal (reqres mode, real SeanerBUS)
	SEANERBUS_HOST=$(SEANERBUS_SERVER) SEANERBUS_PORT=$(SEANERBUS_PORT) \
	SEANERBUS_MODE=reqres \
	SEANERBUS_VECTOR_UUID=$(SEANERBUS_VECTOR_UUID) \
	.venv/bin/python platform/clients/seanerbus_bridge.py

seanerbus-test-req: ## Send one req/res inference request to JPCP (uses JPCP UUID from jpcp.yaml)
	.venv/bin/python platform/clients/seanerbus_test_req.py

monitoring-down: ## Stop Prometheus · Grafana · Loki · Promtail · Alertmanager · Tempo
	@cd $(COMPOSE_DIR) && $(DC) --profile monitoring stop prometheus grafana loki promtail alertmanager tempo
	@cd $(COMPOSE_DIR) && $(DC) --profile monitoring rm -f prometheus grafana loki promtail alertmanager tempo
	@printf "$(DIM)Monitoring stopped.$(RESET)\n"

# =============================================================================
##@ Dashboard
# =============================================================================

dashboard-up: ## Start dashboard service (port 18099)
	@cd $(COMPOSE_DIR) && $(DC) up -d --build dashboard
	@printf "$(GREEN)Dashboard:$(RESET) http://localhost:18099\n"

dashboard-logs: ## Tail dashboard container logs
	@cd $(COMPOSE_DIR) && $(DC) logs -f dashboard

dashboard-check: ## Run dashboard backend + frontend tests
	@printf "$(BOLD)Backend tests...$(RESET)\n"
	@cd platform/services/dashboard/backend && \
	  pip install -r requirements.txt -q && \
	  pytest tests/ -v --tb=short
	@printf "$(BOLD)Frontend tests...$(RESET)\n"
	@cd platform/services/dashboard/frontend && npm ci -q && npm test
	@printf "$(GREEN)Dashboard checks passed.$(RESET)\n"

# =============================================================================
##@ JupyterHub  (multi-user notebook server — Phase 9)
# =============================================================================

jupyter-up: ## Build images + start JupyterHub multi-user notebook server (port 18888)
	@printf "$(BOLD)Building JupyterLab user image...$(RESET)\n"
	@docker build --network=host -t examlops-jupyterlab -f $(COMPOSE_DIR)/Dockerfile.jupyterlab $(COMPOSE_DIR)
	@printf "$(BOLD)Starting JupyterHub...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) --profile jupyter up -d --build jupyterhub
	@printf "\n$(GREEN)JupyterHub is up:$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "JupyterHub UI" "http://localhost:18888  (login with your account)"
	@printf "\n$(DIM)VS Code: Jupyter extension → Specify Server → http://localhost:18888/user/<name>/?token=<token>$(RESET)\n\n"

jupyter-down: ## Stop JupyterHub (user volumes are preserved)
	@cd $(COMPOSE_DIR) && $(DC) --profile jupyter stop jupyterhub
	@cd $(COMPOSE_DIR) && $(DC) --profile jupyter rm -f jupyterhub
	@printf "$(DIM)JupyterHub stopped. User home volumes preserved.$(RESET)\n"

jupyter-logs: ## Tail JupyterHub logs
	@cd $(COMPOSE_DIR) && $(DC) --profile jupyter logs -f jupyterhub

jupyter-add-user: ## Add a JupyterHub user  USER=<name>  HUB_TOKEN=<admin-api-token>
	@[ -n "$(USER)" ] || (printf "$(RED)Error: USER= is required  (e.g. make jupyter-add-user USER=alice HUB_TOKEN=<token>)$(RESET)\n"; exit 1)
	@curl -sf -X POST http://localhost:18888/hub/api/users/$(USER) \
	  -H "Authorization: token $${HUB_TOKEN:?HUB_TOKEN= is required — generate at http://localhost:18888/hub/token}" \
	  | python3 -m json.tool
	@printf "$(GREEN)User '$(USER)' created. Set password via: http://localhost:18888/hub/admin$(RESET)\n"

# Training, Prefect deployments, Ray Serve checks/reloads, approvals, ModelZoo
# status, and model scaffolding live in the `exa` CLI. Keep those workflows
# documented in `exa --help` and docs/reference/cli.md so there is one source
# of truth for copy-paste operator examples.

# =============================================================================
##@ Control Plane  (retraining trigger API)
# =============================================================================

control-plane-up: ## Start the control plane on port 18002 (uses Prefect at :14200)
	@cd $(COMPOSE_DIR) && $(DC) up -d --build control-plane
	@printf "$(GREEN)Control plane:$(RESET) http://localhost:18002  (POST /retrain · GET /retrain/{id} · GET /models · GET /approvals)\n"

control-plane-down: ## Stop the control plane container
	@cd $(COMPOSE_DIR) && $(DC) stop control-plane && $(DC) rm -f control-plane
	@printf "$(DIM)Control plane stopped.$(RESET)\n"

control-plane-logs: ## Tail control plane logs
	@cd $(COMPOSE_DIR) && $(DC) logs -f control-plane

# =============================================================================
##@ Firewall fix  (lxp-cpu01 self-healing Docker egress — stopgap)
# =============================================================================
FIREWALL_FIX_DIR := platform/infra/firewall-fix

firewall-fix-up: ## Start self-healing Docker-egress sidecar (lxp-cpu01; Docker access only, no sudo)
	@chmod +x $(FIREWALL_FIX_DIR)/ensure-egress.sh
	@cd $(FIREWALL_FIX_DIR) && docker compose up -d
	@printf "$(BOLD)firewall-fix running.$(RESET) Re-applies the nft egress rule across firewalld reloads / docker restarts.\n"

firewall-fix-down: ## Stop the self-healing Docker-egress sidecar
	@cd $(FIREWALL_FIX_DIR) && docker compose down

firewall-fix-logs: ## Tail the firewall-fix sidecar (shows each rule (re-)apply)
	@cd $(FIREWALL_FIX_DIR) && docker compose logs -f

# =============================================================================
##@ Agent  (LangGraph + Ollama management CLI)
# =============================================================================

agent: install ## Start the ExaMLOps management agent CLI (backend: Azure/Claude API or Ollama)
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	$(PYTHON) platform/services/agent/agent.py

# =============================================================================
##@ Python Environment
# =============================================================================

venv: _guard-uv _guard-python ## Create .venv with uv (no-op if it already exists)
	@test -d $(VENV) || $(UV) venv $(VENV) --python $(PYTHON_MIN)
	@printf "$(GREEN)$(VENV) ready$(RESET)  (Python: $$($(PYTHON) --version))\n"

install: venv ## Install runtime dependencies into .venv
	@$(UV) pip install -e . -q
	@printf "$(GREEN)Runtime dependencies installed.$(RESET)\n"

install-dev: venv ## Install runtime + dev dependencies (pytest · ruff · mypy)
	@$(UV) pip install -e ".[dev]" -q
	@printf "$(GREEN)Runtime + dev dependencies installed.$(RESET)\n"

clean: ## Remove .venv, build artifacts, and all cache directories
	@rm -rf $(VENV) platform/cli/src/*.egg-info
	@find . -type d \( -name __pycache__ -o -name .pytest_cache \
	       -o -name .mypy_cache -o -name .ruff_cache \) \
	  -not -path '*/.git/*' -exec rm -rf {} + 2>/dev/null || true
	@printf "$(GREEN)Clean.$(RESET)\n"

# =============================================================================
##@ Code Quality
# =============================================================================

lint: install-dev ## Run ruff linter across src/, tests/, pipelines/, platform/services/
	@printf "$(BOLD)Linting...$(RESET)\n"
	@$(VENV)/bin/ruff check platform/cli/src/ tests/ pipelines/ serving/ platform/services/
	@printf "$(GREEN)Lint passed.$(RESET)\n"

lint-fix: install-dev ## Run ruff --fix (auto-fix all safe issues)
	@$(VENV)/bin/ruff check --fix platform/cli/src/ tests/ pipelines/ serving/ platform/services/
	@printf "$(GREEN)Auto-fix complete.$(RESET)\n"

typecheck: install-dev ## Run mypy type checker on pipelines/ and platform/services/
	@printf "$(BOLD)Type checking...$(RESET)\n"
	@$(VENV)/bin/mypy pipelines/ serving/ platform/services/ --ignore-missing-imports
	@printf "$(GREEN)Type check passed.$(RESET)\n"

test: install-dev ## Run the full test suite
	@$(VENV)/bin/pytest tests/ -v --tb=short

test-unit: install-dev ## Run unit tests only
	@$(VENV)/bin/pytest tests/unit/ -v --tb=short

test-integration: install-dev ## Run integration tests only
	@$(VENV)/bin/pytest tests/integration/ -v --tb=short

test-cov: install-dev ## Run tests with HTML coverage report → htmlcov/index.html
	@$(VENV)/bin/pytest tests/ \
	  --cov=src --cov=pipelines \
	  --cov-report=html --cov-report=term-missing
	@printf "$(GREEN)Coverage report: htmlcov/index.html$(RESET)\n"

check: lint typecheck test dashboard-check ## Run all quality checks: lint · typecheck · test · dashboard
	@printf "\n$(GREEN)$(BOLD)All checks passed.$(RESET)\n\n"

ci: ci-modelzoo ci-infra ci-examlops ## Run all three CI job groups locally (mirrors GitLab CI)
	@printf "\n$(GREEN)$(BOLD)All CI job groups passed locally.$(RESET)\n\n"

smoke-check: ## Run post-deploy health probes against the local stack
	@printf "$(BOLD)Smoke check$(RESET)  (local stack)\n"
	@EXAMLOPS_DEPLOY_PATH=$(CURDIR) bash platform/ci/smoke_check.sh

selfheal: ## Run the container self-healer once against the local stack
	@printf "$(BOLD)Self-heal$(RESET)  (local stack)\n"
	@EXAMLOPS_DEPLOY_PATH=$(CURDIR) bash platform/ci/self_heal.sh

ci-modelzoo: ## Mirror GitHub 'modelzoo' job — poetry install + lint + unit + smoke
	@printf "$(BOLD)CI · modelzoo (poetry)$(RESET)\n"
	@cd $(MODELZOO_DIR) && \
	  (command -v poetry >/dev/null 2>&1 || pip install --quiet poetry) && \
	  poetry config virtualenvs.create false && \
	  poetry install --no-interaction --with dev,ci -q && \
	  ruff check seanergys_modelzoo ci tests && \
	  pytest tests/unit/ tests/smoke/ -v --tb=short
	@printf "$(GREEN)CI · modelzoo passed.$(RESET)\n"

alerts-check: ## Validate Prometheus alert rules with promtool
	@printf "$(BOLD)Validating alert rules...$(RESET)\n"
	@docker run --rm --entrypoint promtool -v "$(CURDIR)/$(COMPOSE_DIR):/cfg" \
	  prom/prometheus:v2.54.1 check rules /cfg/alert_rules.yml
	@printf "$(GREEN)Alert rules valid.$(RESET)\n"

ci-infra: ## Mirror GitHub 'infra' job — compose validation + slurm lint
	@printf "$(BOLD)CI · infra (compose + slurm)$(RESET)\n"
	@$(DC) -f $(COMPOSE_DIR)/docker-compose.yml config --quiet
	@$(DC) -f $(COMPOSE_DIR)/docker-compose.yml --profile monitoring config --quiet
	@$(DC) -f $(COMPOSE_DIR)/docker-compose.yml --profile dev config --quiet
	@$(MAKE) alerts-check
	@command -v ruff >/dev/null 2>&1 || pip install --quiet ruff
	@ruff check platform/infra/slurm-adapter/
	@printf "$(GREEN)CI · infra passed.$(RESET)\n"

ci-examlops: install-dev ## Mirror GitHub 'examlops' job — lint + typecheck + unit
	@printf "$(BOLD)CI · examlops (uv)$(RESET)\n"
	@$(VENV)/bin/ruff check platform/cli/src/ tests/ pipelines/ serving/ platform/services/
	@$(VENV)/bin/mypy pipelines/ serving/ platform/services/ --ignore-missing-imports
	@$(VENV)/bin/pytest tests/unit/ -v --tb=short --no-header -q
	@printf "$(GREEN)CI · examlops passed.$(RESET)\n"

preflight: install-dev ## Full local mirror of every BLOCKING GitLab CI job — run before pushing
	@printf "$(BOLD)Preflight$(RESET)  (mirrors GitLab CI blocking gates)\n"
	@printf "$(BOLD)1/7 sanity: python syntax$(RESET)\n"
	@find platform/ pipelines/ serving/ tests/ tools/ -name "*.py" -print0 \
	  | xargs -0 -r $(VENV)/bin/python -m py_compile
	@printf "$(BOLD)2/7 ruff check$(RESET)\n"
	@$(VENV)/bin/ruff check platform/cli/src/ tests/ pipelines/ serving/ platform/services/
	@printf "$(BOLD)3/7 ruff format --check$(RESET)  (HARD failure in CI)\n"
	@$(VENV)/bin/ruff format --check platform/cli/src/ tests/ pipelines/ serving/ platform/services/
	@printf "$(BOLD)4/7 mypy$(RESET)  (non-blocking, mirrors CI '|| true')\n"
	@$(VENV)/bin/mypy pipelines/ serving/ platform/services/ --ignore-missing-imports || true
	@printf "$(BOLD)5/7 unit tests$(RESET)\n"
	@$(VENV)/bin/pytest tests/unit/ --tb=short -q
	@printf "$(BOLD)6/7 integration tests$(RESET)  (the suite that masked the v0.24.0 regression)\n"
	@$(VENV)/bin/pytest tests/integration/ --tb=short -q
	@printf "$(BOLD)7/7 dashboard backend$(RESET)\n"
	@$(UV) pip install -q -r platform/services/dashboard/backend/requirements.txt
	@cd platform/services/dashboard/backend && \
	  EXAMLOPS_DOCS_ROOT=$(CURDIR) $(CURDIR)/$(VENV)/bin/pytest tests/ --tb=short -q --ignore=tests/test_storage.py
	@$(MAKE) ci-infra
	@printf "\n$(GREEN)$(BOLD)Preflight passed — safe to push.$(RESET)\n"
	@printf "$(DIM)Note: test:modelzoo (poetry) is not run here; use 'make ci-modelzoo' for the upstream gate.$(RESET)\n\n"

# =============================================================================
##@ Documentation  (MkDocs)
# =============================================================================

docs-serve: install-dev ## Serve MkDocs locally at http://localhost:8080 (hot-reload)
	@printf "$(BOLD)MkDocs$(RESET)  →  http://localhost:8080\n"
	@$(UV) pip install -q mkdocs-material mkdocs-minify-plugin 2>/dev/null || true
	@$(VENV)/bin/mkdocs serve --dev-addr 0.0.0.0:8080

docs-build: install-dev ## Build MkDocs static site → site/
	@$(UV) pip install -q mkdocs-material mkdocs-minify-plugin 2>/dev/null || true
	@$(VENV)/bin/mkdocs build --clean
	@printf "$(GREEN)Docs built: site/index.html$(RESET)\n"

# =============================================================================
##@ ModelZoo Tests
# =============================================================================

modelzoo-test: ## Run modelzoo test suite — smoke + unit (uses poetry in modelzoo/)
	@printf "$(BOLD)Running modelzoo tests...$(RESET)\n"
	@cd $(MODELZOO_DIR) && \
	  (command -v poetry >/dev/null 2>&1 \
	    && poetry install --with ci -q 2>/dev/null \
	    || pip install -q -r requirements.txt)
	@cd $(MODELZOO_DIR) && python -m pytest tests/smoke/ tests/unit/ -v --tb=short
	@printf "$(GREEN)ModelZoo tests passed.$(RESET)\n"

agent-test:  ## Run the management-agent unit tests
	.venv/bin/pip install -q langgraph-checkpoint-sqlite langchain-anthropic langchain-ollama anthropic respx fastapi uvicorn
	.venv/bin/pytest platform/services/agent/tests -v

agent-server: install ## Start the ExaMLOps agent web chat UI (port 18004)
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	printf "$(BOLD)ExaMLOps Agent Chat$(RESET)  →  http://localhost:$${AGENT_SERVER_PORT:-18004}\n"; \
	$(PYTHON) platform/services/agent/agent_server.py

# =============================================================================
##@ Convenience
# =============================================================================

bootstrap: touch-env-dashboard stack-up install-dev ## One-shot infrastructure setup: stack + dependencies
	@printf "\n$(GREEN)$(BOLD)Bootstrap complete.$(RESET)\n\n"
	@printf "  Dashboard requires DASHBOARD_VIEWER_PASSWORD, DASHBOARD_ADMIN_PASSWORD,\n"
	@printf "  DASHBOARD_JWT_SECRET, DASHBOARD_SECRET_KEY in .env. See .env.example.\n\n"
	@printf "  Next steps:\n"
	@printf "    exa pipeline list        → see registered models\n"
	@printf "    exa pipeline run --dummy → train all models (dummy data, instant)\n"
	@printf "    exa status               → verify services and platform state\n"
	@printf "    make check               → run all quality checks\n\n"

# =============================================================================
# Internal guards — not shown in help
# =============================================================================

_guard-uv:
	@command -v $(UV) >/dev/null 2>&1 || { \
	  printf "$(RED)$(BOLD)Error:$(RESET)$(RED) 'uv' not found.\n"; \
	  printf "Install:  curl -LsSf https://astral.sh/uv/install.sh | sh$(RESET)\n"; \
	  exit 1; }

_guard-python:
	@python3 -c "import sys; v=sys.version_info; exit(0 if (v.major,v.minor)>=(3,12) else 1)" \
	  2>/dev/null || { \
	  printf "$(RED)$(BOLD)Error:$(RESET)$(RED) Python 3.12+ required.\n"; \
	  printf "Found:    $$(python3 --version 2>&1 || echo 'python3 not found')$(RESET)\n"; \
	  exit 1; }

_guard-service:
	@test -n "$(SERVICE)" || { \
	  printf "$(RED)Set SERVICE=<container-name>\n"; \
	  printf "Example:  make stack-shell SERVICE=mlflow$(RESET)\n"; \
	  exit 1; }
