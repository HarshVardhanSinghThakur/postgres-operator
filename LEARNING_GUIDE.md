# Postgres Operator — Learning Guide

This document contains all learning material for understanding the operator codebase.

---

## What Each File Teaches You

### config.py
→ Why constants live in one file. What K8s labels are for.
  Label selectors, how resources find each other.
  Operator versioning, annotation keys for idempotency.

### metrics.py
→ Prometheus data types: Counter vs Gauge vs Histogram.
  Why observability is a first-class concern, not an afterthought.
  Labels for multidimensional queries (namespace, phase, result).

### status.py
→ K8s status conditions convention. Why status subresource exists.
  The infinite reconciliation loop bug and how to avoid it.
  Phase transitions: Creating → Running → Degraded → Terminating.

### provisioner.py
→ The idempotency problem. Hash-based change detection.
  Owner references and garbage collection.
  StatefulSet vs Deployment — why it matters for databases.
  Health probes: liveness vs readiness and what happens when they fail.
  PVC immutability and storage class expansion limits.

### main.py
→ The control loop / reconciliation pattern.
  kopf.TemporaryError vs kopf.PermanentError — when to retry, when to stop.
  The drift problem and why a timer-based reconciler solves it.
  RBAC 403 vs transient API errors — different response needed.
  Event-driven (create/update/delete) + time-driven (reconcile) handlers.

### backup.py
→ Abstract base class pattern for pluggable backends.
  Why the backup container is separate from the operator container.
  pg_dump flags and how Postgres dumps work.
  Retention policy logic using filename timestamps.
  CronJob concurrencyPolicy, backoffLimit, history limits.

### restore.py
→ K8s Job for one-time operations (not CronJob).
  initContainers for backup download (S3/local) before main restore.
  Volume mounts shared between initContainer and main container.
  Status updates on PostgresRestore CR (Pending → Running → Completed/Failed).
  psql restore from compressed stdin.

### rbac.yaml
→ Principle of least privilege. ServiceAccount, ClusterRole, Binding.
  Why operators need RBAC and exactly what permissions they need.
  Status subresource as a separate permission (update/status).

### helm/templates/deployment.yaml
→ Helm templating: values, conditionals, helpers.
  Env vars for operator config (namespace, log level, metrics port).
  Liveness/readiness probes for the operator itself.
  Resource requests/limits for operator pod.

### helm/templates/servicemonitor.yaml
→ ServiceMonitor CRD (Prometheus Operator).
  How Prometheus discovers scrape targets via labels.
  Namespace selectors, endpoint config.

### prometheus/prometheus.yml
→ Prometheus scrape config, rule evaluation.
  ServiceMonitorSelector for auto-discovery.
  Alertmanager integration.

### prometheus/alerting-rules.yaml
→ Alerting rules: operator down, reconcile failures, backup failures.
  For expressions, labels, annotations.
  Severity routing (critical vs warning).

### grafana/dashboard.json
→ Dashboard panels: instance count by phase, reconcile duration heatmap.
  Backup success/failure rates, last backup age.
  Templating for namespace/instance selection.

### Makefile
→ Dev workflow automation: kind cluster, build, deploy, logs, clean.
  Multi-target: dev (local), build (images), deploy (helm), test.

---

## Mental Model: The Operator Pattern

```
                        KUBERNETES CONTROL PLANE
  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
  │   etcd      │◄───│  API Server │───►│  Controller │    │  Scheduler  │
  │  (state)    │    │  (REST)     │    │  Manager    │    │             │
  └─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
                           ▲
                           │ watch/create/patch/delete
                           │
                    ┌──────┴──────┐
                    │ YOUR OPERATOR │  (runs as a Pod, or locally via kopf)
                    │   (kopf)      │
                    └──────┬──────┘
                           │
               ┌────────────┼────────────┐
               ▼            ▼            ▼
         ┌──────────┐ ┌──────────┐ ┌──────────┐
         │ Secret   │ │  PVC     │ │StatefulSet│
         │(password)│ │(storage) │ │(postgres) │
         └──────────┘ └──────────┘ └──────────┘
               ▲            ▲            ▲
               │            │            │
         ┌─────┴─────┐ ┌────┴────┐ ┌─────┴─────┐
         │  Service  │ │ CronJob │ │   Pod     │
         │  (DNS)    │ │ (backup)│ │  (pg)     │
         └───────────┘ └─────────┘ └───────────┘
```

---

## File Flow: What Happens When You Apply a Manifest

```
User: kubectl apply -f my-db.yaml
           │
           ▼
    ┌──────────────────────────────────────────┐
    │  K8s API Server validates against CRD    │
    │  (managedpostgres.yaml schema)           │
    └──────────────────────────────────────────┘
           │
           ▼
    ┌──────────────────────────────────────────┐
    │  kopf watches for ADDED event            │
    │  Fires @kopf.on.create handler           │
    └──────────────────────────────────────────┘
           │
           ▼
    ┌──────────────────────────────────────────┐
    │  main.py:on_create()                     │
    │  1. StatusManager.mark_creating()        │
    │  2. Provisioner(namespace, name, uid,    │
    │                  spec, api_version)      │
    │  3. p.ensure_secret()   → creates Secret │
    │  4. p.ensure_pvc()      → creates PVC    │
    │  5. p.ensure_statefulset() → creates STS │
    │  6. p.ensure_service()  → creates Svc    │
    │  7. p.ensure_backup_cronjob() (optional) │
    │  8. StatusManager.mark_running(endpoint) │
    └──────────────────────────────────────────┘
           │
           ▼
    ┌──────────────────────────────────────────┐
    │  Every 60s: @kopf.timer fires reconcile()│
    │  Same Provisioner calls → hash compare   │
    │  Only patches if spec actually changed   │
    │  Recreates missing resources (self-heal) │
    └──────────────────────────────────────────┘
```

---

## How Backup Works

```
┌─────────────────────────────────────────────────────────────────┐
│                        CRONJOB (runs daily)                     │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  BACKUP CONTAINER (backup/backup.py)                     │  │
│  │  1. Reads env: POSTGRES_HOST, PASSWORD, DB_NAME, BACKEND │  │
│  │  2. Runs: pg_dump -h $HOST -U postgres -d $DB -F p       │  │
│  │       │ gzip > /tmp/backup.sql.gz                         │  │
│  │  3. Uploads via BACKEND:                                  │  │
│  │       LOCAL  → copy to /backups/<db>/ (needs PVC mount)   │  │
│  │       S3     → boto3 upload_file() to s3://bucket/...     │  │
│  │  4. Lists backups, deletes > RETENTION_DAYS (default 7)   │  │
│  │  5. Exits 0 (success) or 1 (failure → CronJob retries)   │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

Key design decisions:
- Separate container — operator doesn't need pg_dump, boto3, or DB clients
- Pluggable backend — add GCS/Azure by writing one class + elif in factory
- Retention by filename timestamp — no external metadata store needed

---

## How Reconciliation Works

```
┌─────────────────────────────────────────────────────────────────┐
│                     RECONCILIATION LOOP                         │
│                                                                 │
│  @kopf.timer(interval=60s) ──► reconcile()                      │
│       │                                                         │
│       ▼                                                         │
│  For each resource:  GET → hash compare → PATCH if changed      │
│       │                                                         │
│       ├── Secret:     only metadata (NEVER touch password)     │
│       ├── PVC:        storage size (may fail if no expansion)  │
│       ├── StatefulSet: image, resources, env                   │
│       ├── Service:    selector, ports                          │
│       └── CronJob:    schedule, image, env                     │
│       │                                                         │
│       ▼                                                         │
│  If ANY resource was recreated → log "Drift detected and      │
│  healed" + update status                                        │
│                                                                 │
│  METRICS: reconcile_duration, reconcile_total{result=noop|     │
│  healed|failure}                                                │
└─────────────────────────────────────────────────────────────────┘
```

Why timer-based? Events only fire when ManagedPostgres changes. If someone manually deletes the StatefulSet, no event fires. The timer catches this drift.

---

## Senior-Level Patterns in This Codebase

| Pattern | Location | Why It Matters |
|---------|----------|----------------|
| Idempotent reconciliation | provisioner.py:112-124 | resource_exists() + needs_update() = safe retries |
| Hash-based change detection | provisioner.py:67-77, 127-134 | Avoids unnecessary patches, prevents drift |
| Owner references + GC | provisioner.py:92-109 | No manual delete handlers needed |
| Pluggable backend (ABC) | backup.py:79-103 | Add GCS/Azure without touching core logic |
| Status subresource | CRDs + main.py | Prevents infinite reconciliation loops |
| Kopf error semantics | main.py:166-199 | TemporaryError vs PermanentError = correct retry behavior |
| Metrics-first design | metrics.py | Counters/Gauges/Histograms with meaningful labels |
| Multi-stage Dockerfiles | Both Dockerfiles | Small images, no build tools in runtime |
| Single source of truth | config.py | All labels, annotations, defaults in one place |

---

## Learning Path: How to Study This Project

### Phase 1: Trace a Create Request (30 min)
```bash
cd operator && python main.py
kubectl apply -f ../examples/manifests.yaml
# Watch operator logs for CREATE → Secret → PVC → StatefulSet → Service → Running
```

### Phase 2: Trace Reconciliation / Self-Healing (15 min)
```bash
kubectl delete statefulset my-app-db
# Wait 60s → watch logs: "Drift detected and healed: {'statefulset': 'created'}"
```

### Phase 3: Read Code in This Order
1. `config.py` — All constants (10 min)
2. `provisioner.py` — Core logic, read `ensure_secret()` first (30 min)
3. `main.py` — Handlers, error handling (30 min)
4. `backup/backup.py` — Pluggable backend pattern (20 min)
5. `metrics.py` — Prometheus metric types (15 min)
6. CRDs — Schema validation, status subresource (15 min)
7. `status.py` — Status conditions, phase transitions (15 min)
8. `restore.py` — Job pattern, initContainers (20 min)

### Phase 4: Modify Something to Verify Understanding
- Change `RETRY_INITIAL_DELAY` in `config.py` → see faster/slower retries
- Add a new metric in `metrics.py` → see it at `/metrics`
- Change `backupSchedule` in example → watch CronJob update
- Delete PVC → watch recreation (if StorageClass supports it)

---

## Build Order (What Was Written When)

### Turn 1 (Foundation):
- crds/managedpostgres.yaml, crds/postgresrestore.yaml
- operator/config.py, operator/metrics.py, operator/provisioner.py
- backup/backup.py, backup/Dockerfile, backup/requirements.txt
- operator.Dockerfile, requirements.txt

### Turn 2 (Core Logic):
- operator/main.py
- helm/postgres-operator/templates/rbac.yaml
- helm/postgres-operator/values.yaml
- examples/manifests.yaml

### Turn 3 (Restore + Helm):
- operator/status.py
- operator/restore.py
- helm/postgres-operator/Chart.yaml
- helm/postgres-operator/templates/deployment.yaml
- helm/postgres-operator/templates/servicemonitor.yaml

### Turn 4 (Observability + DX):
- README.md
- prometheus/prometheus.yml, prometheus/alerting-rules.yaml
- grafana/dashboard.json
- Makefile

---

## How to Run Locally Right Now (no cloud needed)

### 1. Install tools
```bash
brew install kind kubectl helm         # mac
# or: apt install kubectl + kind from GitHub releases
```

### 2. Start local cluster
```bash
kind create cluster --name pg-operator
```

### 3. Register CRDs
```bash
kubectl apply -f crds/
```

### 4. Apply RBAC
```bash
kubectl apply -f helm/postgres-operator/templates/rbac.yaml
```

### 5. Install Python deps
```bash
pip install -r requirements.txt
```

### 6. Run operator locally (outside cluster — easiest for dev)
```bash
cd operator
python main.py
# Operator connects to kind cluster via ~/.kube/config
```

### 7. In another terminal — apply a test resource
```bash
kubectl apply -f examples/manifests.yaml
```

### 8. Watch what happens
```bash
kubectl get managedpostgres
kubectl describe managedpostgres my-app-db
kubectl get pods
kubectl get pvc
kubectl get secrets
```

### 9. Check metrics
```bash
curl http://localhost:8080/metrics
```

### 10. Simulate drift — delete the StatefulSet manually
```bash
kubectl delete statefulset my-app-db
# Wait 60 seconds — reconciler recreates it automatically
kubectl get statefulset   # back!
```

### 11. Test backup
```bash
kubectl get cronjob
kubectl create job --from=cronjob/my-app-db-backup manual-backup
kubectl logs job/manual-backup
```

### 12. Test restore
```bash
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
```