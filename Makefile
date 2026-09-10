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
# deploy-node layout: $(DEPLOY_PATH)-seanerbus/seanerbus
# laptop layout:      ../../seanerbus  (two levels up from repo root)
SEANERBUS_DIR   ?= $(or $(wildcard $(ROOT_DIR)/../examlops-seanerbus/seanerbus),$(ROOT_DIR)/../../seanerbus)
PID_DIR         := .run

# ── Remote deploy node ────────────────────────────────────────────────────────
# Site-specific. Set the real values in .env (gitignored) or the environment:
#   EXAMLOPS_DEPLOY_HOST=<ssh-host>   EXAMLOPS_DEPLOY_PATH=/path/to/checkout
DEPLOY_HOST     ?= $(or $(EXAMLOPS_DEPLOY_HOST),examlops-deploy)
DEPLOY_PATH     ?= $(or $(EXAMLOPS_DEPLOY_PATH),/opt/examlops)

# ── Docker Compose  ───────────────────────────────────────────────────────────
# .env lives at the repo root; compose runs from COMPOSE_DIR so we must pass
# --env-file explicitly (docker compose only auto-loads .env from CWD).
DC := docker compose --env-file $(ROOT_DIR)/.env

# ── Python / uv ───────────────────────────────────────────────────────────────
VENV   := .venv
PYTHON := $(VENV)/bin/python
UV     := uv
# Absolute, so a recipe that `cd`s into a sub-project still reaches THIS venv. A bare
# `pip`/`pytest` there resolves against whatever happens to be on PATH: on a PEP-668 host
# the gate dies for a reason unrelated to the change under test, and on a host where the
# system Python is writable it silently tests a different interpreter and dependency set.
VENV_BIN := $(CURDIR)/$(VENV)/bin

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

.PHONY: images helm-package
.PHONY: help \
        full-up stop-all rebuild rebuild-all lxp-rebuild \
        stack-up stack-down stack-wipe stack-restart stack-logs stack-ps stack-shell \
        touch-env-dashboard \
        monitoring-up monitoring-down \
        seanerbus-up seanerbus-down seanerbus-bridge-logs seanerbus-reqgen-logs \
        seanerbus-install seanerbus-bridge-up seanerbus-test-req \
        dashboard-up dashboard-logs dashboard-check dashboard-check-backend ci-frontend ci-control-plane \
        jupyter-up jupyter-down jupyter-logs jupyter-add-user \
        control-plane-up control-plane-down control-plane-logs \
        firewall-fix-up firewall-fix-down firewall-fix-logs \
        agent agent-chat agent-server \
        skipper skipper-chat skipper-server skipper-test skipper-memory \
        finops-providers finops-plugin-example \
        remote-rebuild selfheal smoke-check \
        test-postgres \
        venv install install-dev install-hooks clean \
        lint lint-fix typecheck typecheck-cli typecheck-fast openapi-export test test-unit test-integration test-cov check \
        test-fast test-failed test-serial test-slowest watch gate \
        alerts-check dr-drill helm-validate \
        ci ci-modelzoo ci-infra ci-examlops ci-agent \
        preflight preflight-nopg \
        modelzoo-test agent-test \
        docs-serve docs-build docs-cli \
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

stack-up: _guard-uv ## Start core stack: Postgres · MLflow · Prefect · Ray · Control Plane · Agent · Dashboard
	@printf "$(BOLD)Starting ExaMLOps core stack...$(RESET)\n"
	@cd $(COMPOSE_DIR) && $(DC) up -d --build
	@printf "\n$(GREEN)Core stack is up:$(RESET)\n"
	@printf "  %-30s %s\n" \
	  "Dashboard"     "http://localhost:18099" \
	  "MLflow UI"     "http://localhost:15000" \
	  "Prefect UI"    "http://localhost:14200" \
	  "Ray Serve API" "http://localhost:18001  (/docs · /health · /models)" \
	  "Ray Dashboard" "http://localhost:18265"
	@printf "  %-30s %s\n" "Skipper Agent" "http://localhost:18004  (/healthz)"
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

remote-rebuild: ## Pull latest code + force-rebuild + restart all containers on the deploy node
	@printf "$(BOLD)Rebuilding on $(DEPLOY_HOST)...$(RESET)\n"
	@ssh $(DEPLOY_HOST) "set -e; \
	  cd $(DEPLOY_PATH); \
	  echo '=== git pull ==='; \
	  git pull; \
	  cd platform/infra/docker-compose; \
	  echo '=== stopping all containers ==='; \
	  docker compose --env-file $(DEPLOY_PATH)/.env \
	    --profile monitoring --profile jupyter --profile seanerbus \
	    down --remove-orphans 2>/dev/null || true; \
	  echo '=== rebuilding all images (no cache) ==='; \
	  docker compose --env-file $(DEPLOY_PATH)/.env \
	    --profile monitoring build --no-cache; \
	  echo '=== starting core stack ==='; \
	  docker compose --env-file $(DEPLOY_PATH)/.env up -d; \
	  echo '=== starting monitoring stack ==='; \
	  docker compose --env-file $(DEPLOY_PATH)/.env \
	    --profile monitoring up -d prometheus grafana loki promtail alertmanager tempo; \
	  echo '=== done ==='; \
	  docker compose --env-file $(DEPLOY_PATH)/.env ps"

lxp-rebuild: remote-rebuild ## Deprecated alias for remote-rebuild
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
	@# `2>/dev/null || (stack not running)` diagnosed *every* compose failure as a stopped
	@# stack and still exited 0 — including the interpolation error that made `ps` itself
	@# impossible. Show what compose actually said before guessing why.
	@# SHELL carries -e, so `out=$$(cmd); rc=$$?` would abort before the handler ever runs.
	@cd $(COMPOSE_DIR) && { out=$$($(DC) ps 2>&1) && rc=0 || rc=$$?; }; \
	  if [ $$rc -eq 0 ]; then \
	    printf '%s\n' "$$out"; \
	    [ "$$(printf '%s\n' "$$out" | tail -n +2 | grep -c .)" -gt 0 ] || \
	      printf "  $(DIM)(no containers running — run 'make stack-up')$(RESET)\n"; \
	  else \
	    printf "  $(RED)docker compose could not read the stack:$(RESET)\n"; \
	    printf '%s\n' "$$out" | sed 's/^/    /'; \
	    exit $$rc; \
	  fi

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

seanerbus-install: install ## Install SeanerBUS Python bindings from the configured sibling checkout
	@test -d "$(SEANERBUS_DIR)/bindings/python" || { \
	  printf "$(RED)ERROR: SeanerBUS Python bindings not found under $(SEANERBUS_DIR).$(RESET)\n"; \
	  exit 1; \
	}
	$(UV) pip install -e "$(SEANERBUS_DIR)/bindings/python"

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
	@test -x $(VENV_BIN)/pytest || { \
	  printf "$(RED)No $(VENV)/bin/pytest — run 'make install-dev' first.$(RESET)\n"; exit 1; }
	@cd platform/services/dashboard/backend && \
	  $(VENV_BIN)/pip install -r requirements.txt -q && \
	  $(VENV_BIN)/pytest tests/ $(PYTEST_PARALLEL) --tb=short -q
	@printf "$(BOLD)Frontend tests...$(RESET)\n"
	@command -v npm >/dev/null 2>&1 || { \
	  printf "$(RED)npm not on PATH — the frontend half of this gate cannot run.$(RESET)\n"; \
	  printf "$(RED)Install node, or run 'make dashboard-check-backend' and say so explicitly.$(RESET)\n"; \
	  exit 1; }
	@cd platform/services/dashboard/frontend && npm ci -q && npm run lint && npm test && npm run build
	@printf "$(GREEN)Dashboard checks passed (backend + frontend).$(RESET)\n"

dashboard-check-backend: ## Dashboard BACKEND tests only (use when there is no node on the host)
	@printf "$(BOLD)Backend tests (frontend deliberately skipped)...$(RESET)\n"
	@test -x $(VENV_BIN)/pytest || { \
	  printf "$(RED)No $(VENV)/bin/pytest — run 'make install-dev' first.$(RESET)\n"; exit 1; }
	@cd platform/services/dashboard/backend && \
	  $(VENV_BIN)/pip install -r requirements.txt -q && \
	  $(VENV_BIN)/pytest tests/ $(PYTEST_PARALLEL) --tb=short -q
	@printf "$(GREEN)Dashboard BACKEND checks passed — the frontend half did NOT run.$(RESET)\n"

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
##@ Skipper  (LangGraph management agent — CLI, web, OpenAI-compatible bridge)
# =============================================================================

skipper: install ## Start Skipper, the ExaMLOps management agent CLI (backend: Azure/Claude API or Ollama)
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	$(PYTHON) platform/services/agent/agent.py

agent: skipper ## Alias for `skipper` (backward compatibility)

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

# NEVER `cp` over .git/hooks/pre-push. On this machine that filename is owned by the global
# AI-attribution guard (tag messages, --no-verify commits, CHANGELOG/CONTRIBUTORS scanning),
# installed from infra-hub into every repo. This target used to overwrite it, which would have
# silently removed the last line of defence before anything leaves the machine — the one hook
# whose whole job is to be un-bypassable. Both gits are covered: `.git` (public) and
# `.git-private` each have their own hooks directory.
install-hooks: ## Install the repo's pre-push CI gate into every git dir, without clobbering the attribution guard
	@for d in .git .git-private; do \
	  [ -d "$$d/hooks" ] || continue; \
	  cp platform/ci/hooks/pre-push "$$d/hooks/pre-push-ci"; \
	  chmod +x "$$d/hooks/pre-push-ci"; \
	  if [ ! -e "$$d/hooks/pre-push" ]; then \
	    cp platform/ci/hooks/pre-push "$$d/hooks/pre-push"; \
	    chmod +x "$$d/hooks/pre-push"; \
	    printf "$(GREEN)%s/hooks/pre-push installed$(RESET) (no existing hook)\n" "$$d"; \
	  elif grep -q "pre-push-ci" "$$d/hooks/pre-push" 2>/dev/null; then \
	    printf "$(DIM)%s/hooks/pre-push already chains the CI gate — refreshed.$(RESET)\n" "$$d"; \
	  else \
	    printf "$(BOLD)%s/hooks/pre-push exists and is NOT ours — left untouched.$(RESET)\n" "$$d"; \
	    printf "  The CI gate is installed beside it as $$d/hooks/pre-push-ci.\n"; \
	    printf "  To run both, add this as the LAST line of $$d/hooks/pre-push:\n"; \
	    printf "    exec \"\$$(git rev-parse --git-dir)/hooks/pre-push-ci\" \"\$$@\"\n"; \
	  fi; \
	done
	@printf "$(DIM)Gate: py-compile + ruff check + ruff format --check + unit tests (parallel, ~70s).$(RESET)\n"

clean: ## Remove .venv, build artifacts, and all cache directories
	@rm -rf $(VENV) platform/cli/src/*.egg-info
	@find . -type d \( -name __pycache__ -o -name .pytest_cache \
	       -o -name .mypy_cache -o -name .ruff_cache \) \
	  -not -path '*/.git/*' -exec rm -rf {} + 2>/dev/null || true
	@printf "$(GREEN)Clean.$(RESET)\n"

# =============================================================================
##@ Code Quality
# =============================================================================

lint: install-dev ## Run ruff linter across cli/, tests/, pipelines/, serving/, services/, clients/, usecases/
	@printf "$(BOLD)Linting...$(RESET)\n"
	@$(VENV)/bin/ruff check platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/
	@# `ruff format --check` is a HARD failure in CI's test:examlops and was NOT run here, so
	@# `make check` could report green on a tree that CI rejects on formatting alone. That is
	@# exactly what produced the v0.26.1 -> v0.27.1 red-pipeline saga, and it recurred on
	@# 2026-08-20 (two test files landed unformatted and no local gate noticed).
	@$(VENV)/bin/ruff format --check platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/
	@printf "$(GREEN)Lint passed.$(RESET)\n"

lint-fix: install-dev ## Run ruff --fix (auto-fix all safe issues)
	@$(VENV)/bin/ruff check --fix platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/
	@$(VENV)/bin/ruff format platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/
	@printf "$(GREEN)Auto-fix complete.$(RESET)\n"

# `platform/cli/src/` — the `examlops` package, 243 source files — is not yet mypy-clean, so it
# is **ratcheted, not skipped**: the error count may only go down. Skipping it is what let a real
# `arg-type` error sit in `autopilot_cmd` with this gate reporting green. Lower the baseline
# whenever the recipe tells you it fell; never raise it.
CLI_MYPY_BASELINE ?= 0

typecheck: install-dev ## Run mypy on pipelines/, serving/, platform/services/ + ratcheted platform/cli/src/
	@printf "$(BOLD)Type checking...$(RESET)\n"
	@$(VENV)/bin/mypy pipelines/ serving/ platform/services/ --ignore-missing-imports
	@$(MAKE) --no-print-directory typecheck-cli
	@printf "$(GREEN)Type check passed.$(RESET)\n"

typecheck-cli: ## mypy over platform/cli/src/, ratcheted at CLI_MYPY_BASELINE (one source of truth)
	@printf "$(BOLD)Type checking platform/cli/src/ (ratchet: $(CLI_MYPY_BASELINE))...$(RESET)\n"
	@n=$$($(VENV)/bin/mypy platform/cli/src/ --ignore-missing-imports | grep -c "^platform/cli/src/.*error:" || true); \
	if [ "$$n" -gt "$(CLI_MYPY_BASELINE)" ]; then \
		printf "$(RED)platform/cli/src/ has $$n mypy errors; the ratchet is $(CLI_MYPY_BASELINE). Fix the new ones.$(RESET)\n"; \
		$(VENV)/bin/mypy platform/cli/src/ --ignore-missing-imports || true; \
		exit 1; \
	elif [ "$$n" -lt "$(CLI_MYPY_BASELINE)" ]; then \
		printf "$(GREEN)platform/cli/src/ is down to $$n errors — lower CLI_MYPY_BASELINE to $$n.$(RESET)\n"; \
	else \
		printf "$(DIM)platform/cli/src/ holds at $$n known errors.$(RESET)\n"; \
	fi

# The committed api-contract.json is the control plane's public API contract; its guard test
# (platform/services/control_plane/tests/test_openapi_contract.py) fails on any drift, so an
# interface change is always a reviewed diff, never a runtime surprise. The reduction in
# api_contract.py is what keeps the guard portable across fastapi versions.
openapi-export: install-dev ## Regenerate the control plane's committed API contract (api-contract.json)
	@$(VENV_BIN)/python platform/services/control_plane/api_contract.py
	@printf "$(GREEN)platform/services/control_plane/api-contract.json regenerated — review the diff.$(RESET)\n"

# The daemon's first run builds its cache (minutes, same as cold mypy); every run after an
# edit is seconds. Same four roots and the same flag as `typecheck`, so a clean fast run
# means the slow gate's mypy body is clean too (the CLI ratchet is 0, i.e. clean-enforced).
# dmypy 2.3 exits 1 when only `annotation-unchecked` *notes* are present (plain mypy exits
# 0 on the same tree), so pass = the "Success: no issues" line, not the raw exit code; a
# daemon crash has neither the line nor exit 0 and still fails.
typecheck-fast: install-dev ## Incremental mypy via the dmypy daemon — seconds per re-check once warm
	@out=$$($(VENV)/bin/dmypy run -- pipelines/ serving/ platform/services/ platform/cli/src/ --ignore-missing-imports 2>&1); st=$$?; \
	printf '%s\n' "$$out" | grep -v 'annotation-unchecked' || true; \
	if [ $$st -ne 0 ] && ! printf '%s\n' "$$out" | grep -q '^Success: no issues'; then exit $$st; fi

# ── Test tiers ───────────────────────────────────────────────────────────────
# The suite is 2781 unit tests. Run serially that is ~9 minutes, which is long enough that the
# gate gets skipped — and a gate that gets skipped is not a gate. Across this machine's cores it
# is ~66s, so the *whole* suite is affordable on every change and no test-selection heuristic is
# needed to make the inner loop fast. That is the trade deliberately taken here: impact-based
# selection would shave another minute and would silently miss the many guard tests in this repo
# that read files rather than import them.
#
#   make test-fast   ~70s   inner loop / pre-commit — whole unit suite, parallel, quiet
#   make gate        ~2min  pre-push — lint + format + typecheck + test-fast + docs
#   make preflight   ~10min pre-release — full CI mirror incl. Postgres, dashboard, compose
#
# JOBS is overridable: `make test-fast JOBS=4` on a loaded laptop, `JOBS=0` to force serial when
# a failure is suspected of being an isolation bug rather than a real one.
JOBS ?= auto
PYTEST_PARALLEL = $(if $(filter 0,$(JOBS)),,-n $(JOBS))

test: install-dev ## Run the full test suite (unit + integration, parallel)
	@$(VENV)/bin/pytest tests/ $(PYTEST_PARALLEL) --tb=short

test-fast: install-dev ## TIER 1 (~70s) — whole unit suite in parallel; the inner-loop gate
	@printf "$(BOLD)Unit suite (parallel, JOBS=$(JOBS))...$(RESET)\n"
	@$(VENV)/bin/pytest tests/unit/ $(PYTEST_PARALLEL) -q --tb=short --no-header

test-failed: install-dev ## Re-run only the tests that failed last time (then the rest)
	@$(VENV)/bin/pytest tests/unit/ $(PYTEST_PARALLEL) -q --tb=short --no-header --last-failed \
	  --last-failed-no-failures all

# The layer below `test-fast`: keep it running while editing and every save re-runs the
# scope, failed-first, without the ~10s of collection+spinup a fresh `make test-fast` pays.
# Scope it to the area being worked on — the whole unit tree on every save is what
# `test-fast` is for, before a commit.
watch: install-dev ## Re-run tests on every save (Ctrl+C stops). Scope: make watch W=tests/unit/test_x.py
	@$(VENV)/bin/ptw --now --delay 0.5 --runner $(VENV)/bin/pytest . \
	  -- $(or $(W),tests/unit/) -q --tb=short --ff

test-unit: install-dev ## Run unit tests only (verbose, parallel)
	@$(VENV)/bin/pytest tests/unit/ $(PYTEST_PARALLEL) -v --tb=short

test-serial: install-dev ## Run the unit suite single-process — to confirm a parallel-only failure
	@$(VENV)/bin/pytest tests/unit/ -q --tb=short --no-header

test-slowest: install-dev ## Show the 30 slowest tests — find what to mark `slow` or fix
	@$(VENV)/bin/pytest tests/unit/ $(PYTEST_PARALLEL) -q --tb=no --no-header --durations=30

test-integration: install-dev ## Run integration tests only
	@$(VENV)/bin/pytest tests/integration/ -v --tb=short

PGTEST_CONTAINER ?= examlops-pgtest
PGTEST_PORT      ?= 15433
PGTEST_DSN       ?= postgresql://examlops:examlops@localhost:$(PGTEST_PORT)/examlops

test-postgres: install-dev ## Run the unit + dashboard suites against a throwaway Postgres (item 0.1 parity)
	@printf "$(BOLD)Starting $(PGTEST_CONTAINER) on port $(PGTEST_PORT)...$(RESET)\n"
	@docker rm -f $(PGTEST_CONTAINER) >/dev/null 2>&1 || true
	@docker run -d --name $(PGTEST_CONTAINER) \
	  -e POSTGRES_PASSWORD=examlops -e POSTGRES_USER=examlops -e POSTGRES_DB=examlops \
	  -p $(PGTEST_PORT):5432 postgres:16-alpine >/dev/null
	@until docker exec $(PGTEST_CONTAINER) pg_isready -U examlops >/dev/null 2>&1; do sleep 1; done
	@# SHELL carries -e, so the original `pytest …; status=$$?` aborted on the FIRST failing
	@# suite: the dashboard suite and the live integration test never ran, the summed exit
	@# code was never computed, and `docker rm -f` — the last line — never fired, leaving the
	@# throwaway Postgres up indefinitely. `|| status=$$?` keeps each failure local, and the
	@# trap removes the container however the recipe ends, including on Ctrl-C.
	@trap 'docker rm -f $(PGTEST_CONTAINER) >/dev/null 2>&1 || true' EXIT INT TERM; \
	 status=0; dash=0; live=0; \
	 EXAMLOPS_DB_BACKEND=postgres EXAMLOPS_POSTGRES_DSN='$(PGTEST_DSN)' \
	 EXAMLOPS_POSTGRES_SCHEMA=exa_test $(VENV)/bin/pytest tests/unit/ -q || status=$$?; \
	 (cd platform/services/dashboard/backend && \
	  EXAMLOPS_DB_BACKEND=postgres EXAMLOPS_POSTGRES_DSN='$(PGTEST_DSN)' \
	  EXAMLOPS_POSTGRES_SCHEMA=exa_test_dash \
	  PYTHONPATH=$(CURDIR)/platform/cli/src \
	  $(CURDIR)/$(VENV)/bin/pytest tests/ -q) || dash=$$?; \
	 EXAMLOPS_POSTGRES_TEST_DSN='$(PGTEST_DSN)' $(VENV)/bin/pytest tests/integration/test_postgres_backend_live.py -q || live=$$?; \
	 exit $$((status + dash + live))

test-cov: install-dev ## Run tests with HTML coverage report → htmlcov/index.html
	@$(VENV)/bin/pytest tests/ \
	  --cov=src --cov=pipelines \
	  --cov-report=html --cov-report=term-missing
	@printf "$(GREEN)Coverage report: htmlcov/index.html$(RESET)\n"

gate: install-dev ## TIER 2 (~2min) — the pre-push gate: lint · format · typecheck · unit · docs
	@printf "$(BOLD)Gate 1/5 lint$(RESET)\n"
	@$(VENV)/bin/ruff check platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/
	@printf "$(BOLD)Gate 2/5 format$(RESET)\n"
	@$(VENV)/bin/ruff format --check platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/
	@printf "$(BOLD)Gate 3/5 typecheck$(RESET)\n"
	@$(MAKE) --no-print-directory typecheck-cli
	@printf "$(BOLD)Gate 4/5 unit tests$(RESET)\n"
	@$(MAKE) --no-print-directory test-fast
	@printf "$(BOLD)Gate 5/5 docs$(RESET)\n"
	@$(MAKE) --no-print-directory docs-build
	@printf "\n$(GREEN)$(BOLD)Gate passed — safe to push.$(RESET)\n\n"

check: lint typecheck test dashboard-check ## Run all quality checks: lint · typecheck · test · dashboard
	@printf "\n$(GREEN)$(BOLD)All checks passed.$(RESET)\n\n"

ci: ci-modelzoo ci-infra ci-examlops ci-agent ci-frontend ci-control-plane ## Run every CI job group locally (mirrors GitLab CI)
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
	  (command -v poetry >/dev/null 2>&1 \
	    || uv tool install poetry >/dev/null 2>&1 \
	    || pipx install poetry >/dev/null 2>&1 \
	    || pip install --quiet poetry) && \
	  P="env -u VIRTUAL_ENV poetry" && \
	  { $$P env use "$(CURDIR)/$(PYTHON)" >/dev/null 2>&1 || $$P env use python3.12 >/dev/null 2>&1 || true; } && \
	  $$P install --no-interaction --with dev,ci -q && \
	  $$P run ruff check seanergys_modelzoo ci tests && \
	  $$P run pytest tests/unit/ tests/smoke/ -v --tb=short
	@printf "$(GREEN)CI · modelzoo passed.$(RESET)\n"

alerts-check: ## Validate Prometheus alert rules + Alertmanager config
	@printf "$(BOLD)Validating alert rules...$(RESET)\n"
	@docker run --rm --entrypoint promtool -v "$(CURDIR)/$(COMPOSE_DIR):/cfg" \
	  prom/prometheus:v2.54.1 check rules /cfg/alert_rules.yml
	@printf "$(BOLD)Validating Alertmanager config...$(RESET)\n"
	@docker run --rm --entrypoint amtool -v "$(CURDIR)/$(COMPOSE_DIR)/alertmanager.yml:/tmp/am.yml:ro" \
	  prom/alertmanager:v0.27.0 check-config /tmp/am.yml
	@printf "$(GREEN)Alert rules + Alertmanager config valid.$(RESET)\n"

# A throwaway registry for validation only — it is never pulled from. The chart REQUIRES
# global.imageRegistry (see templates/_helpers.tpl), so rendering without one is an error.
HELM_VALIDATE_REGISTRY ?= ghcr.io/example/

# Where the built chart repo will be served from. Baked into index.yaml, so it must match the
# final host; override per publish target. No decision is implied by the default.
HELM_REPO_URL ?= https://mskazemi.github.io/ExaMLOps

# The three tiers the Helm chart deploys. Tag = the chart's appVersion, which the chart
# defaults every image tag to, so these must agree; tests/unit/test_helm_chart.py holds
# appVersion to the platform version and this reads the same source.
IMAGE_TAG ?= $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
IMAGE_PREFIX ?=

images: ## Build the three container images the Helm chart deploys (control-plane, dashboard, agent)
	@printf "$(BOLD)Building ExaMLOps images at tag $(IMAGE_TAG)...$(RESET)\n"
	docker build -f platform/services/control_plane/Dockerfile -t $(IMAGE_PREFIX)examlops-control-plane:$(IMAGE_TAG) .
	docker build -f platform/infra/docker-compose/Dockerfile.dashboard -t $(IMAGE_PREFIX)examlops-dashboard:$(IMAGE_TAG) .
	docker build -f platform/services/agent/Dockerfile -t $(IMAGE_PREFIX)examlops-agent:$(IMAGE_TAG) .
	@printf "$(GREEN)Built 3 images at $(IMAGE_TAG).$(RESET)\n"
	@printf "  Push with IMAGE_PREFIX=ghcr.io/<owner>/ make images && docker push ...\n"
	@printf "  Then: helm install ... --set global.imageRegistry=ghcr.io/<owner>/\n"

helm-package: helm-validate ## Build the chart tarball + index.yaml into dist/helm (a publishable Helm repo)
	@rm -rf dist/helm && mkdir -p dist/helm
	@helm package platform/infra/helm/examlops -d dist/helm
	@helm repo index dist/helm --url $(HELM_REPO_URL)
	@printf "$(GREEN)Helm repo built in dist/helm.$(RESET)\n"
	@printf "  Publishing = copying that directory to $(HELM_REPO_URL) (gh-pages or any static host).\n"
	@printf "  Consumers then run:  helm repo add examlops $(HELM_REPO_URL)\n"
	@printf "$(YELLOW)  Images must exist first — the chart requires global.imageRegistry.$(RESET)\n"

helm-validate: ## Lint + render + schema-validate the enterprise Helm chart (item 1.1)
	@printf "$(BOLD)helm lint...$(RESET)\n"
	@helm lint platform/infra/helm/examlops --set global.imageRegistry=$(HELM_VALIDATE_REGISTRY)
	@printf "$(BOLD)chart refuses to render without a registry...$(RESET)\n"
	@if helm template rel platform/infra/helm/examlops >/dev/null 2>&1; then \
		printf "$(RED)FAIL: the chart rendered with no global.imageRegistry — it would emit docker.io/library/ refs.$(RESET)\n"; \
		exit 1; \
	fi
	@printf "$(BOLD)helm template...$(RESET)\n"
	@helm template rel platform/infra/helm/examlops --set global.imageRegistry=$(HELM_VALIDATE_REGISTRY) >/dev/null
	@if kubectl cluster-info --request-timeout=3s >/dev/null 2>&1; then \
		printf "$(BOLD)kubectl schema check...$(RESET)\n"; \
		helm template rel platform/infra/helm/examlops --set global.imageRegistry=$(HELM_VALIDATE_REGISTRY) \
			| kubectl apply --dry-run=client -f - >/dev/null; \
		printf "$(GREEN)Helm chart valid (lint + refusal check + render + kubectl dry-run).$(RESET)\n"; \
	else \
		printf "$(YELLOW)No cluster reachable - SKIPPED the kubectl schema check.$(RESET)\n"; \
		printf "$(YELLOW)  'kubectl apply --dry-run=client' downloads the OpenAPI schema from a live$(RESET)\n"; \
		printf "$(YELLOW)  apiserver, so it is not an offline check. Structure is covered by$(RESET)\n"; \
		printf "$(YELLOW)  tests/unit/test_helm_chart.py; run this target against a cluster for schemas.$(RESET)\n"; \
		printf "$(GREEN)Helm chart valid (lint + refusal check + render).$(RESET)\n"; \
	fi

dr-drill: install-dev ## Disaster-recovery drill — backup → wipe → restore round-trip (item 0.9)
	@printf "$(BOLD)Running DR drill (single-DB + whole-platform bundle round-trip)...$(RESET)\n"
	@.venv/bin/pytest tests/unit/test_backup_restore.py tests/unit/test_backup_bundle.py \
		tests/unit/test_backup_tiers.py tests/unit/test_backup_ops.py -q
	@printf "$(GREEN)DR drill passed — restore path verified (RPO=last backup, RTO=restore time).$(RESET)\n"

ci-infra: ## Mirror GitHub 'infra' job — compose validation + slurm lint
	@printf "$(BOLD)CI · infra (compose + slurm)$(RESET)\n"
	@# The EXAMLOPS_VLLM_MODEL=... prefix these lines carried was a workaround for a
	@# required-variable expression on a profile-gated service; the compose file no
	@# longer needs one, and leaving it here would hide a regression from the gate.
	@$(DC) -f $(COMPOSE_DIR)/docker-compose.yml config --quiet
	@$(DC) -f $(COMPOSE_DIR)/docker-compose.yml --profile monitoring config --quiet
	@$(DC) -f $(COMPOSE_DIR)/docker-compose.yml --profile dev config --quiet
	@$(DC) -f $(COMPOSE_DIR)/docker-compose.yml --profile vllm config --quiet
	@$(MAKE) alerts-check
	@if [ -x $(VENV)/bin/ruff ]; then $(VENV)/bin/ruff check platform/infra/slurm-adapter/; \
	  else ruff check platform/infra/slurm-adapter/; fi
	@printf "$(GREEN)CI · infra passed.$(RESET)\n"

ci-examlops: install-dev ## Mirror GitHub 'examlops' job — lint + typecheck + unit
	@printf "$(BOLD)CI · examlops (uv)$(RESET)\n"
	@$(VENV)/bin/ruff check platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/
	@$(VENV)/bin/mypy pipelines/ serving/ platform/services/ --ignore-missing-imports
	@$(MAKE) --no-print-directory typecheck-cli
	@# `-n auto` mirrors the GitHub job, which runs it too. A "CI mirror" that runs the suite
	@# differently from CI is the thing this target exists to prevent. (`-v` and `-q` were both
	@# passed here, which is contradictory; `-q` won, so only `-q` is kept.)
	@$(VENV)/bin/pytest tests/unit/ -n auto --tb=short --no-header -q
	@printf "$(GREEN)CI · examlops passed.$(RESET)\n"

ci-control-plane: ## Mirror GitLab 'test:control-plane' job — the control plane service's own tests
	@printf "$(BOLD)CI · control plane$(RESET)\n"
	@$(VENV_BIN)/pytest platform/services/control_plane/tests $(PYTEST_PARALLEL) --tb=short -q
	@printf "$(GREEN)CI · control plane passed.$(RESET)\n"

ci-frontend: ## Mirror GitLab 'test:frontend' job — dashboard frontend lint + vitest + tsc build
	@printf "$(BOLD)CI · dashboard frontend (npm)$(RESET)\n"
	@command -v npm >/dev/null 2>&1 || { \
	  printf "$(RED)npm not on PATH — cannot mirror the test:frontend gate.$(RESET)\n"; exit 1; }
	@cd platform/services/dashboard/frontend && npm ci -q && npm run lint && npm test && npm run build
	@printf "$(GREEN)CI · dashboard frontend passed.$(RESET)\n"

ci-agent: install-dev ## Mirror the 'test:agent' job — the Skipper agent suite
	@printf "$(BOLD)CI · agent (skipper)$(RESET)\n"
	@$(MAKE) --no-print-directory skipper-test
	@printf "$(GREEN)CI · agent passed.$(RESET)\n"

preflight: install-dev ## Full local mirror of every BLOCKING GitLab CI job — run before pushing
	@printf "$(BOLD)Preflight$(RESET)  (mirrors GitLab CI blocking gates)\n"
	@printf "$(BOLD)1/16 sanity: python syntax$(RESET)\n"
	@find platform/ pipelines/ serving/ tests/ tools/ -name "*.py" \
	  -not -path "*/node_modules/*" -not -path "*/.venv/*" -print0 \
	  | xargs -0 -r $(VENV)/bin/python -m py_compile
	@printf "$(BOLD)2/16 sanity: repo structure$(RESET)\n"
	@test -f pyproject.toml
	@test -d platform/cli/src/examlops
	@test -d platform/services/dashboard
	@test -d platform/services/control_plane
	@test -f platform/infra/docker-compose/docker-compose.yml
	@test -d usecases/seanergy/models
	@printf "$(BOLD)3/16 sanity: secret scan$(RESET)\n"
	@$(VENV)/bin/exa secrets scan platform/
	@$(VENV)/bin/exa secrets scan pipelines/
	@printf "$(BOLD)4/16 ruff check$(RESET)\n"
	@$(VENV)/bin/ruff check platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/
	@printf "$(BOLD)5/16 ruff format --check$(RESET)  (HARD failure in CI)\n"
	@$(VENV)/bin/ruff format --check platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/
	@printf "$(BOLD)6/16 mypy$(RESET)  (HARD failure in CI)\n"
	@$(VENV)/bin/mypy pipelines/ serving/ platform/services/ --ignore-missing-imports
	@printf "$(BOLD)7/16 unit tests$(RESET)\n"
	@$(VENV)/bin/pytest tests/unit/ --tb=short -q
	@printf "$(BOLD)8/16 integration tests$(RESET)  (the suite that masked the v0.24.0 regression)\n"
	@$(VENV)/bin/pytest tests/integration/ --tb=short -q
	@printf "$(BOLD)9/16 dashboard backend$(RESET)\n"
	@$(UV) pip install -q -r platform/services/dashboard/backend/requirements.txt
	@cd platform/services/dashboard/backend && \
	  EXAMLOPS_DOCS_ROOT=$(CURDIR) $(CURDIR)/$(VENV)/bin/pytest tests/ --tb=short -q
	@printf "$(BOLD)10/16 skipper agent tests$(RESET)  (blocking in CI since the test:agent job)\n"
	@$(MAKE) --no-print-directory skipper-test
	@printf "$(BOLD)11/16 dashboard frontend$(RESET)  (blocking in CI since the test:frontend job)\n"
	@$(MAKE) --no-print-directory ci-frontend
	@printf "$(BOLD)12/16 control plane$(RESET)  (blocking in CI since the test:control-plane job)\n"
	@$(MAKE) --no-print-directory ci-control-plane
	@printf "$(BOLD)13/16 infra$(RESET)  (compose + slurm-lint + alert-rules)\n"
	@$(MAKE) --no-print-directory ci-infra
	@printf "$(BOLD)14/16 helm chart$(RESET)  (blocking in CI since the test:infra:helm job)\n"
	@if command -v helm >/dev/null 2>&1; then \
	  $(MAKE) --no-print-directory helm-validate; \
	  $(MAKE) --no-print-directory helm-package; \
	else \
	  printf "$(RED)helm is not installed — the blocking job test:infra:helm was NOT mirrored.$(RESET)\n"; \
	  printf "$(RED)Install it (https://helm.sh/docs/intro/install/) so the chart gate runs here too.$(RESET)\n"; \
	  exit 1; \
	fi
	@printf "$(BOLD)15/16 docs site$(RESET)  (blocking in CI since the test:docs job)\n"
	@$(MAKE) --no-print-directory docs-build
	@printf "$(BOLD)16/16 postgres backend$(RESET)  (the whole suite again on Postgres — slow; needs docker)\n"
	@if [ -n "$(PREFLIGHT_SKIP_PG)" ]; then \
	  printf "$(RED)SKIPPED by PREFLIGHT_SKIP_PG — the blocking job test:postgres was NOT mirrored.$(RESET)\n"; \
	else \
	  docker info >/dev/null 2>&1 || { \
	    printf "$(RED)No docker daemon — test:postgres is a BLOCKING CI job and cannot be mirrored here.$(RESET)\n"; \
	    printf "$(RED)Start docker, or run 'make preflight-nopg', which says out loud that it did not run.$(RESET)\n"; \
	    exit 1; }; \
	  $(MAKE) --no-print-directory test-postgres; \
	fi
	@if [ -n "$(PREFLIGHT_SKIP_PG)" ]; then \
	  printf "\n$(BOLD)$(RED)Preflight incomplete — test:postgres did not run. Say so before you push.$(RESET)\n"; \
	else \
	  printf "\n$(GREEN)$(BOLD)Preflight passed — safe to push.$(RESET)\n"; \
	fi
	@printf "$(DIM)Note: test:modelzoo (poetry) is not run here; use 'make ci-modelzoo' for the upstream gate.$(RESET)\n\n"

preflight-nopg: ## Preflight WITHOUT the Postgres mirror (only when there is no docker daemon)
	@$(MAKE) --no-print-directory preflight PREFLIGHT_SKIP_PG=1


# =============================================================================
##@ Documentation  (MkDocs)
# =============================================================================

docs-serve: install-dev ## Serve MkDocs locally at http://localhost:8080 (hot-reload)
	@printf "$(BOLD)MkDocs$(RESET)  →  http://localhost:8080\n"
	@$(UV) pip install -q mkdocs-material mkdocs-minify-plugin 2>/dev/null || true
	@$(VENV)/bin/mkdocs serve --dev-addr 0.0.0.0:8080

docs-build: install-dev ## Build MkDocs static site → site/ (strict: a broken link fails)
	@$(UV) pip install -q mkdocs-material mkdocs-minify-plugin 2>/dev/null || true
	@# --strict turns mkdocs' link warnings into failures. Twenty guides once linked to
	@# design/adr/*.md, which is outside docs_dir and never published, so the built site
	@# shipped twenty dead links and the build said nothing. Warnings are zero as of
	@# 2026-08-23; keep it that way by failing here rather than in a reader's browser.
	@$(VENV)/bin/mkdocs build --clean --strict
	@printf "$(GREEN)Docs built: site/index.html$(RESET)\n"

docs-cli: install-dev ## Regenerate the full CLI reference from the live command tree
	@$(VENV)/bin/exa docs --out docs/reference/cli-generated.md
	@printf "$(GREEN)CLI reference regenerated: docs/reference/cli-generated.md$(RESET)\n"

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

skipper-test:  ## Run the Skipper agent unit tests
	@# Install from requirements.txt, the pinned source of truth. The hardcoded list that
	@# used to live here had already drifted from it (no httpx, uvicorn without [standard]),
	@# which is what a second copy of a dependency set always does.
	@$(VENV)/bin/pip install -q -r platform/services/agent/requirements.txt
	@$(VENV)/bin/pip install -q pytest-asyncio
	@$(VENV)/bin/pytest platform/services/agent/tests $(PYTEST_PARALLEL) -q

agent-test: skipper-test  ## Alias for `skipper-test` (backward compatibility)

skipper-server: install ## Start Skipper's web UI + OpenAI-compatible bridge (port 18004)
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	printf "$(BOLD)Skipper (ExaMLOps agent)$(RESET)  →  http://localhost:$${AGENT_SERVER_PORT:-18004}\n"; \
	$(PYTHON) platform/services/agent/agent_server.py

agent-server: skipper-server ## Alias for `skipper-server` (backward compatibility)

skipper-chat: install ## Chat with Skipper via the native `exa chat` client
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	AGENT_URL="$${AGENT_URL:-http://localhost:$${AGENT_SERVER_PORT:-18004}}" \
	$(VENV_BIN)/exa chat

agent-chat: skipper-chat ## Alias for `skipper-chat` (backward compatibility)

skipper-memory: ## Admin Skipper's long-term memory (stats|list|export|delete); e.g. make skipper-memory ARGS=stats
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	cd platform/services/agent && $(PWD)/$(PYTHON) -m skipper.memory_admin $${ARGS:-stats}

.PHONY: skipper-knowledge-ingest
skipper-knowledge-ingest: ## Chunk+embed the docs into Skipper's knowledge tier (T2 docs-RAG)
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	cd platform/services/agent && $(PWD)/$(PYTHON) -m skipper.knowledge ingest $${ARGS:-}

.PHONY: skipper-watch
skipper-watch: ## Run one skipper-watch monitoring cycle (drift/cost → outbox+audit+memory); ARGS=--dry-run
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	cd platform/services/agent && $(PWD)/$(PYTHON) -m skipper.watch --once $${ARGS:-}

.PHONY: skipper-consolidate
skipper-consolidate: ## Offline memory reflection: promote recurring episodes (review-gated) + reinforce
	@set -a; [ -f .env ] && . ./.env || true; set +a; \
	cd platform/services/agent && $(PWD)/$(PYTHON) -m skipper.consolidate $${ARGS:-}

# =============================================================================
##@ Convenience
# =============================================================================

finops-providers: install-dev ## List available carbon calculation providers (built-ins + plugins)
	@$(VENV)/bin/exa finops carbon providers

finops-plugin-example: install-dev ## Install the example carbon provider plugin (examples/exa-carbon-plugin)
	@$(UV) pip install ./examples/exa-carbon-plugin
	@printf "$(GREEN)Installed. Try: exa finops carbon estimate --gpu-hours 12 --provider example-fixed-grid$(RESET)\n"

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
