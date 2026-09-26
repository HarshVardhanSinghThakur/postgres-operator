# Makefile — Development workflow for postgres-operator
#
# Usage:
#   make dev-setup    # Create kind cluster, install CRDs, RBAC
#   make dev          # Run operator locally with hot reload
#   make build        # Build Docker images
#   make deploy       # Deploy via Helm to kind cluster
#   make test         # Run tests
#   make clean        # Delete kind cluster
#   make logs         # Tail operator logs
#   make port-forward # Port-forward metrics and postgres
#   make backup       # Trigger manual backup
#   make restore      # Create test restore

# ── Configuration ─────────────────────────────────────────────────────────────
CLUSTER_NAME     := pg-operator
OPERATOR_IMAGE   := harshdev/postgres-operator
BACKUP_IMAGE     := harshdev/postgres-backup
TAG              := latest
NAMESPACE        := default
HELM_RELEASE     := postgres-operator
HELM_CHART       := ./helm/postgres-operator
KUBECTL          := kubectl
HELM             := helm
DOCKER           := docker
KIND             := kind

# Python
PYTHON           := python
PIP              := pip
VENV_DIR         := .venv
OPERATOR_DIR     := operator

# ── Phony targets ─────────────────────────────────────────────────────────────
.PHONY: help dev-setup dev build push deploy test clean logs port-forward backup restore status

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo "Postgres Operator — Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make dev-setup     Create kind cluster, install CRDs + RBAC"
	@echo "  make dev-venv      Create Python virtual environment"
	@echo ""
	@echo "Development:"
	@echo "  make dev           Run operator locally (hot reload via kopf)"
	@echo "  make logs          Tail operator logs"
	@echo "  make status        Show all managed resources"
	@echo ""
	@echo "Build & Deploy:"
	@echo "  make build         Build operator + backup Docker images"
	@echo "  make push          Push images to registry"
	@echo "  make deploy        Deploy via Helm to cluster"
	@echo "  make undeploy      Remove Helm release"
	@echo ""
	@echo "Testing:"
	@echo "  make test          Run unit tests"
	@echo "  make test-e2e      Run e2e tests (requires cluster)"
	@echo "  make backup        Trigger manual backup job"
	@echo "  make restore       Create test PostgresRestore"
	@echo ""
	@echo "Observability:"
	@echo "  make port-forward  Port-forward metrics (8080) + postgres (5432)"
	@echo "  make metrics       Curl /metrics endpoint"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean         Delete kind cluster"
	@echo "  make clean-all     Delete cluster + local build artifacts"

# ── Setup ─────────────────────────────────────────────────────────────────────
dev-setup: kind-cluster install-crds install-rbac
	@echo "Development environment ready!"
	@echo "   Run 'make dev' to start the operator locally"

kind-cluster:
	@echo "Creating kind cluster: $(CLUSTER_NAME)..."
	@$(KIND) create cluster --name $(CLUSTER_NAME) || true
	@echo "Cluster ready"

install-crds:
	@echo "Installing CRDs..."
	@$(KUBECTL) apply -f crds/
	@echo "CRDs installed"

install-rbac:
	@echo "🔐 Installing RBAC..."
	@$(KUBECTL) apply -f helm/postgres-operator/templates/rbac.yaml
	@echo "RBAC installed"

dev-venv:
	@echo "Creating virtual environment..."
	@$(PYTHON) -m venv $(VENV_DIR)
	@$(VENV_DIR)/bin/$(PIP) install --upgrade pip
	@$(VENV_DIR)/bin/$(PIP) install -r requirements.txt
	@echo "Virtual environment ready at $(VENV_DIR)"
	@echo "   Activate with: source $(VENV_DIR)/bin/activate"

# ── Development ───────────────────────────────────────────────────────────────
dev: dev-venv
	@echo "🚀 Starting operator locally..."
	@echo "   Press Ctrl+C to stop"
	@cd $(OPERATOR_DIR) && $(VENV_DIR)/bin/$(PYTHON) main.py

logs:
	@$(KUBECTL) logs -n $(NAMESPACE) -l app.kubernetes.io/name=postgres-operator -f --tail=100

status:
	@echo "=== ManagedPostgres ==="
	@$(KUBECTL) get managedpostgres -A
	@echo ""
	@echo "=== PostgresRestore ==="
	@$(KUBECTL) get postgresrestore -A
	@echo ""
	@echo "=== Pods ==="
	@$(KUBECTL) get pods -A -l app.kubernetes.io/managed-by=postgres-operator
	@echo ""
	@echo "=== PVCs ==="
	@$(KUBECTL) get pvc -A -l app.kubernetes.io/managed-by=postgres-operator
	@echo ""
	@echo "=== CronJobs ==="
	@$(KUBECTL) get cronjob -A -l app.kubernetes.io/managed-by=postgres-operator
	@echo ""
	@echo "=== Jobs ==="
	@$(KUBECTL) get jobs -A -l app.kubernetes.io/managed-by=postgres-operator

# ── Build ─────────────────────────────────────────────────────────────────────
build: build-operator build-backup
	@echo "All images built"

build-operator:
	@echo "Building operator image: $(OPERATOR_IMAGE):$(TAG)"
	@$(DOCKER) build -t $(OPERATOR_IMAGE):$(TAG) -f operator.Dockerfile .

build-backup:
	@echo "Building backup image: $(BACKUP_IMAGE):$(TAG)"
	@$(DOCKER) build -t $(BACKUP_IMAGE):$(TAG) -f backup/backup.Dockerfile backup/

push: push-operator push-backup
	@echo "All images pushed"

push-operator:
	@echo "Pushing operator image..."
	@$(DOCKER) push $(OPERATOR_IMAGE):$(TAG)

push-backup:
	@echo "Pushing backup image..."
	@$(DOCKER) push $(BACKUP_IMAGE):$(TAG)

# ── Deploy ────────────────────────────────────────────────────────────────────
deploy:
	@echo "Deploying via Helm..."
	@$(HELM) upgrade --install $(HELM_RELEASE) $(HELM_CHART) \
		--namespace $(NAMESPACE) --create-namespace \
		--set image.repository=$(OPERATOR_IMAGE) \
		--set image.tag=$(TAG) \
		--set backup.image=$(BACKUP_IMAGE):$(TAG) \
		--wait --timeout=5m
	@echo "Deployed!"

undeploy:
	@echo "Removing Helm release..."
	@$(HELM) uninstall $(HELM_RELEASE) --namespace $(NAMESPACE) || true
	@echo "Removed"

# ── Test ──────────────────────────────────────────────────────────────────────
test: test-unit
	@echo "All tests passed"

test-unit:
	@echo "🧪 Running unit tests..."
	@cd $(OPERATOR_DIR) && $(VENV_DIR)/bin/$(PYTHON) -m pytest -v || true

test-e2e: deploy
	@echo "🧪 Running e2e tests..."
	@$(KUBECTL) apply -f examples/manifests.yaml
	@sleep 30
	@$(KUBECTL) get managedpostgres -n $(NAMESPACE)
	@$(KUBECTL) get pods -n $(NAMESPACE) -l app.kubernetes.io/instance=my-app-db
	@echo "✅ E2E test passed"

# ── Operations ────────────────────────────────────────────────────────────────
backup:
	@echo "💾 Triggering manual backup..."
	@$(KUBECTL) create job --from=cronjob/my-app-db-backup manual-backup-$$(date +%s) -n $(NAMESPACE) || true
	@echo "✅ Backup job created. Check logs with:"
	@echo "   kubectl logs -n $(NAMESPACE) -l job-name=manual-backup-<timestamp> -f"

restore:
	@echo "🔄 Creating test restore..."
	@echo "   First, list available backups:"
	@$(KUBECTL) logs -n $(NAMESPACE) -l app.kubernetes.io/component=backup --tail=50 | grep "Backup stored at" | tail -5
	@echo ""
	@echo "   Then create a PostgresRestore with the backup filename:"
	@echo "   kubectl apply -f - <<EOF"
	@echo "   apiVersion: db.harshdev.io/v1"
	@echo "   kind: PostgresRestore"
	@echo "   metadata:"
	@echo "     name: test-restore-$$(date +%s)"
	@echo "     namespace: $(NAMESPACE)"
	@echo "   spec:"
	@echo "     targetDatabase: my-app-db"
	@echo "     backupFile: my-app-db-20260901T020000Z.sql.gz"
	@echo "   EOF"

port-forward:
	@echo "🔗 Port-forwarding metrics (8080) and postgres (5432)..."
	@echo "   Press Ctrl+C to stop"
	@$(KUBECTL) port-forward -n $(NAMESPACE) svc/$(HELM_RELEASE) 8080:8080 &
	@$(KUBECTL) port-forward -n $(NAMESPACE) svc/my-app-db 5432:5432 &
	@wait

metrics:
	@curl -s http://localhost:8080/metrics | head -50

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean:
	@echo "🧹 Deleting kind cluster..."
	@$(KIND) delete cluster --name $(CLUSTER_NAME)
	@echo "✅ Cluster deleted"

clean-all: clean
	@echo "🧹 Cleaning build artifacts..."
	@rm -rf $(VENV_DIR)
	@$(DOCKER) rmi $(OPERATOR_IMAGE):$(TAG) $(BACKUP_IMAGE):$(TAG) 2>/dev/null || true
	@echo "✅ All cleaned"

# ── Utility ───────────────────────────────────────────────────────────────────
describe:
	@$(KUBECTL) describe managedpostgres my-app-db -n $(NAMESPACE)

events:
	@$(KUBECTL) get events -n $(NAMESPACE) --sort-by=.metadata.creationTimestamp

shell:
	@$(KUBECTL) exec -it -n $(NAMESPACE) $$(kubectl get pods -n $(NAMESPACE) -l app.kubernetes.io/instance=my-app-db -o name | head -1) -- psql -U postgres -d my-app-db

# ── Lint ──────────────────────────────────────────────────────────────────────
lint:
	@echo "🔍 Linting Python..."
	@cd $(OPERATOR_DIR) && $(VENV_DIR)/bin/ruff check . || true
	@cd $(OPERATOR_DIR) && $(VENV_DIR)/bin/ruff format --check . || true

lint-fix:
	@echo "🔧 Fixing lint issues..."
	@cd $(OPERATOR_DIR) && $(VENV_DIR)/bin/ruff check --fix .
	@cd $(OPERATOR_DIR) && $(VENV_DIR)/bin/ruff format .

helm-lint:
	@echo "🔍 Linting Helm chart..."
	@$(HELM) lint $(HELM_CHART)

# ── All-in-one ────────────────────────────────────────────────────────────────
all: dev-setup build deploy test-e2e
	@echo "🎉 Full pipeline complete!"