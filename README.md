# Postgres Operator

A Kubernetes operator for managing PostgreSQL databases with automated backup, restore, and self-healing capabilities.

## Architecture

```mermaid
graph TB
    subgraph "Kubernetes Control Plane"
        API[API Server]
        ETCD[(etcd)]
        CM[Controller Manager]
        SCHED[Scheduler]
    end

    subgraph "Operator Pod"
        OP[postgres-operator<br/>(kopf)]
        METRICS[/metrics :8080]
    end

    subgraph "Managed Resources"
        CRD_MPG[ManagedPostgres CRD]
        CRD_PGR[PostgresRestore CRD]
        MPG[ManagedPostgres<br/>my-app-db]
        PGR[PostgresRestore<br/>restore-1]
    end

    subgraph "Database Resources"
        SEC[Secret<br/>credentials]
        PVC[PVC<br/>postgres-data]
        STS[StatefulSet<br/>postgres]
        SVC[Service<br/>postgres]
        CJ[CronJob<br/>backup]
    end

    subgraph "Backup/Restore"
        BC[Backup Container<br/>pg_dump → S3/Local]
        RC[Restore Job<br/>psql ← S3/Local]
    end

    subgraph "Observability"
        PROM[Prometheus]
        GRAF[Grafana]
        AM[Alertmanager]
    end

    API --> ETCD
    CM --> API
    SCHED --> API
    OP --> API
    OP --> METRICS
    OP -->|watch/create/patch| MPG
    OP -->|watch/create/patch| PGR
    MPG -->|ownerRef| SEC
    MPG -->|ownerRef| PVC
    MPG -->|ownerRef| STS
    MPG -->|ownerRef| SVC
    MPG -->|ownerRef| CJ
    CJ -->|schedule| BC
    PGR -->|create| RC
    RC -->|restore| STS
    BC -->|pg_dump| STS
    METRICS -->|scrape| PROM
    PROM -->|alert| AM
    PROM -->|query| GRAF
```

## Features

- **Declarative PostgreSQL Management**: Define databases via `ManagedPostgres` CR
- **Automated Provisioning**: Creates Secret, PVC, StatefulSet, Service automatically
- **Self-Healing**: Reconciliation loop detects and fixes drift (e.g., manual StatefulSet deletion)
- **Scheduled Backups**: CronJob with pg_dump, supports local PVC and S3 backends
- **Point-in-Time Restore**: `PostgresRestore` CR creates restore Jobs
- **Production-Ready**: Health probes, resource limits, non-root containers, RBAC
- **Observable**: Prometheus metrics, Grafana dashboards, alerting rules
- **Extensible**: Pluggable backup backend architecture

## Quickstart (Local Development)

### Prerequisites

- [kind](https://kind.sigs.k8s.io/) v0.20+
- [kubectl](https://kubernetes.io/docs/tasks/tools/) v1.28+
- [Helm](https://helm.sh/) v3.12+
- Python 3.11+ (for local operator development)
- Docker (for building images)

### 1. Start Local Cluster

```bash
kind create cluster --name pg-operator
```

### 2. Install CRDs and RBAC

```bash
kubectl apply -f crds/
kubectl apply -f helm/postgres-operator/templates/rbac.yaml
```

### 3. Run Operator Locally

```bash
pip install -r requirements.txt
cd operator
python main.py
```

The operator connects to the kind cluster via your `~/.kube/config`.

### 4. Create a Database

```bash
kubectl apply -f examples/manifests.yaml
```

Watch the operator logs — you'll see:
```
CREATE: default/my-app-db
[my-app-db] Secret: created
[my-app-db] PVC: created
[my-app-db] StatefulSet: created
[my-app-db] Service: created
[my-app-db] CREATE complete in 2.34s → my-app-db.default.svc.cluster.local:5432
```

### 5. Verify Database

```bash
kubectl get managedpostgres
kubectl describe managedpostgres my-app-db
kubectl get pods -l app.kubernetes.io/instance=my-app-db
kubectl get pvc,secret,service -l app.kubernetes.io/instance=my-app-db
```

### 6. Connect to Database

```bash
kubectl port-forward svc/my-app-db 5432:5432
# In another terminal:
PGPASSWORD=$(kubectl get secret my-app-db-credentials -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
psql -h localhost -U postgres -d my-app-db
```

### 7. Test Self-Healing (Drift Detection)

```bash
kubectl delete statefulset my-app-db
# Wait ~60 seconds...
kubectl get statefulset my-app-db  # Recreated automatically!
```

Check operator logs:
```
[my-app-db] Drift detected and healed: {'statefulset': 'created'}
```

### 8. Test Backup

```bash
# Enable backup in the ManagedPostgres spec, or trigger manually:
kubectl create job --from=cronjob/my-app-db-backup manual-backup-1
kubectl logs job/manual-backup-1
```

### 9. Test Restore

```bash
# First, find a backup filename from the backup job logs
# Then create a PostgresRestore:
kubectl apply -f - <<EOF
apiVersion: db.harshdev.io/v1
kind: PostgresRestore
metadata:
  name: test-restore
  namespace: default
spec:
  targetDatabase: my-app-db
  backupFile: my-app-db-20260901T020000Z.sql.gz
EOF

kubectl describe postgresrestore test-restore
# Watch phase: Pending → Running → Completed
```

Use `backupFile: latest` to restore the newest backup without looking up the
filename. With `autoRestore: true` (default) the operator creates such a
restore automatically when it detects data loss (re-created instance or
re-created data PVC).

Local backups live in a dedicated `<name>-backup-data` PVC that has no owner
reference, so it survives `kubectl delete managedpostgres`. Deleting the CR
takes a final backup first (finalizer), then unblocks deletion.

### 10. Check Metrics

```bash
curl http://localhost:8080/metrics
```

## Deploy to Cluster (Production)

### Build and Push Images

```bash
# Operator image
docker build -t your-registry/postgres-operator:v1.0.0 -f operator.Dockerfile .
docker push your-registry/postgres-operator:v1.0.0

# Backup image
docker build -t your-registry/postgres-backup:v1.0.0 -f backup/backup.Dockerfile backup/
docker push your-registry/postgres-backup:v1.0.0
```

### Install via Helm

```bash
helm install postgres-operator ./helm/postgres-operator \
  --namespace postgres-operator \
  --create-namespace \
  --set image.repository=your-registry/postgres-operator \
  --set image.tag=v1.0.0
```

### Install Prometheus Stack (for metrics)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace
```

### Install Grafana Dashboard

```bash
kubectl apply -f grafana/dashboard.json
```

## Project Structure

```
postgres-operator/
├── crds/                          # CustomResourceDefinitions
│   ├── managedpostgres.yaml       # ManagedPostgres CRD
│   └── postgresrestore.yaml       # PostgresRestore CRD
├── operator/                      # Operator pod (kopf)
│   ├── config.py                  # Constants, labels, defaults
│   ├── metrics.py                 # Prometheus metrics
│   ├── status.py                  # Status condition management
│   ├── provisioner.py             # Idempotent resource creation
│   ├── main.py                    # kopf handlers (create/update/delete/reconcile)
│   └── restore.py                 # Restore Job implementation
├── backup/                        # Backup CronJob container
│   ├── backup.py                  # pg_dump + pluggable backends
│   ├── Dockerfile                 # Backup container image
│   └── requirements.txt           # boto3
├── helm/
│   └── postgres-operator/         # Helm chart
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           ├── rbac.yaml
│           ├── deployment.yaml
│           └── servicemonitor.yaml
├── examples/
│   └── manifests.yaml             # Example ManagedPostgres resources
├── prometheus/
│   ├── prometheus.yml             # Prometheus scrape config
│   └── alerting-rules.yaml        # Alerting rules
├── grafana/
│   └── dashboard.json             # Grafana dashboard
├── operator.Dockerfile            # Operator container image
├── requirements.txt               # Python dependencies
├── Makefile                       # Development workflow
├── PROJECT_MAP.md                 # Project structure map
└── LEARNING_GUIDE.md              # Learning material, mental models, code walkthrough
```

## ManagedPostgres Spec Reference

```yaml
apiVersion: db.harshdev.io/v1
kind: ManagedPostgres
metadata:
  name: my-app-db
  namespace: default
spec:
  version: "15"              # PostgreSQL version (14, 15, 16)
  storage: "10Gi"            # PVC size
  replicas: 0                # Read replicas (0 = primary only)
  backupEnabled: true        # Enable scheduled backups
  backupSchedule: "0 2 * * *"  # Cron expression (daily at 2 AM)
  backupBackend: "s3"        # "local" or "s3"
  s3Config:                  # Required if backupBackend: s3
    bucket: "my-backups"
    region: "us-east-1"
    credentialsSecret: "aws-creds"  # Secret with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  backupRetentionDays: 7     # How many days of backups to keep
  backupStorage: "5Gi"       # Size of the dedicated backup-data PVC (local backend)
  backupImage: ""            # Backup image override (default: Helm values.backup.image)
  autoRestore: true          # Auto-restore backupFile "latest" when data loss is detected
  resources:
    requests:
      cpu: "500m"
      memory: "512Mi"
    limits:
      cpu: "2000m"
      memory: "2Gi"
```

## Status Fields

```yaml
status:
  phase: Running              # Creating | Running | Degraded | Terminating
  message: "Database is running and healthy"
  endpoint: "my-app-db.default.svc.cluster.local:5432"
  lastBackup: "2026-09-01T02:00:00Z"
  backupCount: 7
  observedGeneration: 5
  conditions:
    - type: DatabaseReady
      status: "True"
      reason: "StatefulSetReady"
      message: "StatefulSet my-app-db is ready"
      lastTransitionTime: "2026-09-01T02:00:00Z"
    - type: BackupConfigured
      status: "True"
      reason: "CronJobCreated"
      message: "Backup scheduled: 0 2 * * *"
      lastTransitionTime: "2026-09-01T02:00:00Z"
    - type: StorageReady
      status: "True"
      reason: "PVCCreated"
      message: "PVC created"
      lastTransitionTime: "2026-09-01T02:00:00Z"
```

## Metrics Reference

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `postgres_operator_instances_total` | Gauge | namespace, phase | Current instances by phase |
| `postgres_operator_instances_created_total` | Counter | namespace | Total creates |
| `postgres_operator_instances_deleted_total` | Counter | namespace | Total deletes |
| `postgres_operator_reconcile_total` | Counter | namespace, result | Reconciliation runs (success/noop/healed/failure) |
| `postgres_operator_reconcile_duration_seconds` | Histogram | namespace | Reconciliation latency |
| `postgres_operator_reconcile_errors_total` | Counter | namespace, error_type | Reconciliation errors |
| `postgres_operator_backups_success_total` | Counter | namespace, instance, backend | Successful backups |
| `postgres_operator_backups_failed_total` | Counter | namespace, instance, backend | Failed backups |
| `postgres_operator_last_backup_timestamp_seconds` | Gauge | namespace, instance | Last backup Unix timestamp |
| `postgres_operator_backup_files_total` | Gauge | namespace, instance | Backup file count |
| `postgres_operator_restores_started_total` | Counter | namespace | Restore operations started |
| `postgres_operator_restores_completed_total` | Counter | namespace | Restore operations completed |
| `postgres_operator_restores_failed_total` | Counter | namespace | Restore operations failed |
| `postgres_operator_restore_duration_seconds` | Histogram | namespace | Restore latency |
| `postgres_operator_k8s_api_calls_total` | Counter | operation, resource, result | K8s API call volume |

## Alerting Rules

Key alerts defined in `prometheus/alerting-rules.yaml`:

- `PostgresOperatorDown` — Operator pod not reporting metrics
- `PostgresOperatorReconcileFailures` — High reconciliation error rate
- `PostgresInstanceDegraded` — Instance in Degraded phase
- `PostgresBackupFailed` — Backup job failed
- `PostgresBackupStale` — No successful backup in 25 hours
- `PostgresRestoreFailed` — Restore job failed

## Development Workflow

```bash
# Start kind cluster and install deps
make dev-setup

# Run operator locally (hot reload on code change)
make dev

# Build images
make build

# Deploy to cluster via Helm
make deploy

# Run tests
make test

# Clean up
make clean
```

## Extending the Operator

### Add a New Backup Backend (e.g., GCS)

1. Create `backup/gcs_backend.py` implementing `BackupBackend` ABC
2. Add `elif config.backend == "gcs": return GCSBackend()` in `get_backend()`
3. Add GCS credentials to CronJob env vars in `provisioner.py`
4. Build and deploy new backup image

### Add Read Replicas

1. Add `replicas` field handling in `provisioner.py:ensure_statefulset()`
2. Implement replication config in pod spec (primary + replica roles)
3. Add Service for read replicas (headless or ClusterIP)
4. Update health probes for replica role

### Add TLS/mTLS

1. Add `tls` section to ManagedPostgres spec
2. Generate certs via cert-manager or operator
3. Mount certs in StatefulSet, configure Postgres `ssl = on`
4. Update Service/connection strings to use TLS

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Operator RBAC errors | Check `ClusterRole` has `create/patch/delete` on `statefulsets`, `secrets`, `pvcs`, `services`, `cronjobs`, `jobs` |
| PVC stuck in Pending | Verify StorageClass exists and supports `ReadWriteOnce` |
| Backup fails with "pg_dump: not found" | Backup image must have `postgresql-client` installed |
| Restore Job stuck | Check initContainer logs — S3 download or PVC copy may have failed |
| Metrics not scraping | Verify ServiceMonitor selector matches Deployment labels |
| Infinite reconcile loop | Ensure CRD has `status` subresource enabled |

## License

MIT License — see LICENSE file for details.