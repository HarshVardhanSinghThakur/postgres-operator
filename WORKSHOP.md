# Postgres Operator — Hands-On Workshop

Learn Kubernetes, Helm, and operator internals by **doing**. Every lab has commands, expected output, and a "why this matters" explanation.

**Your setup:** `kind` cluster named `postgres-dev`, Docker, kubectl, Helm installed.
**Level:** Intermediate (you know Pods, Deployments, Services — we go deep on Controllers, CRDs, StatefulSets, Helm).
**Style:** Mix of topic modules (learn a concept) + problem labs (fix a broken thing).
**Total time:** ~6-8 hours. Do in any order, but Modules 1→2→3 are sequential.

---

## How to Use This File

1. Each Module is independent, but Labs inside a module build on each other.
2. Copy-paste commands in order. Don't skip the **Verify** step.
3. If output doesn't match **Expected Output**, check **Troubleshooting**.
4. Do the **Experiment** at the end of each lab — that's where real learning happens.
5. Track progress with the checklist below.

### Progress Tracker

```text
[ ] 0. Setup & Sanity Check
[ ] 1. CRDs & Custom Resources (1.1 - 1.4)
[ ] 2. Operator Pattern & Reconciliation (2.1 - 2.6)
[ ] 3. Stateful Workloads on K8s (3.1 - 3.5)
[ ] 4. Backup Architecture (4.1 - 4.6)
[ ] 5. Restore & Job Pattern (5.1 - 5.5)
[ ] 6. Helm Packaging (6.1 - 6.6)
[ ] 7. Observability Stack (7.1 - 7.6)
[ ] 8. Production Hardening (8.1 - 8.6)
[ ] 9. Capstone: Production Incident
```

> **Windows PowerShell note:** You are on Windows. All `kubectl`, `helm`, `kind`, `docker` commands are identical. Where Linux uses `$(...)` for command substitution, PowerShell equivalent is noted. `curl` works in PowerShell 7+, otherwise use `Invoke-WebRequest`.

---

## Part 0: Setup & Sanity Check (10 min)

**Concept:** kind cluster context, kubectl connectivity.
**Prereq:** `kind create cluster --name postgres-dev` already done.

### Steps

```powershell
# 1. Verify cluster exists
kind get clusters
# Expected: postgres-dev

# 2. Use the right context
kubectl config use-context kind-postgres-dev
kubectl cluster-info
kubectl get nodes
```

### Expected Output

```text
kind-postgres-dev
Kubernetes control plane is running at https://127.0.0.1:xxxxx
NAME                            STATUS   ROLES           AGE   VERSION
postgres-dev-control-plane      Ready    control-plane   5m    v1.32.x
```

### Verify

```powershell
kubectl get ns
# default, kube-system, kube-public, local-path-storage should exist
```

### Why This Matters

`kind` runs a full K8s control plane (API server + etcd + scheduler + controller-manager) in Docker. Everything you learn here transfers 1:1 to EKS/GKE/AKS. The only difference is storage class and load balancers.

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Unable to connect to the server` | Wrong context | `kubectl config use-context kind-postgres-dev` |
| `kind: command not found` | PATH issue | Reopen terminal, verify install |
| Node `NotReady` | Docker resource pressure | `docker ps`, restart Docker Desktop |

---

## Module 1: CRDs & Custom Resources

**Goal:** Understand how operators extend the K8s API.

Your operator defines two APIs: `ManagedPostgres` and `PostgresRestore` in group `db.harshdev.io`. Without CRDs installed, the API server knows nothing about them.

### Lab 1.1: Install CRDs and Inspect Schema

**Concept:** CRD = API extension. OpenAPI schema = server-side validation.
**Time:** 10 min

```powershell
# From repo root
kubectl apply -f crds/
kubectl get crds | Select-String "db.harshdev"

# Introspect the new API like a native resource
kubectl explain managedpostgres
kubectl explain managedpostgres.spec
kubectl explain managedpostgres.spec --recursive | Select-String "version|storage|backup"
kubectl explain managedpostgres.status
```

**Expected Output:**
```text
customresourcedefinition.apiextensions.k8s.io/managedpostgres.db.harshdev.io created
customresourcedefinition.apiextensions.k8s.io/postgresrestores.db.harshdev.io created

NAME: managedpostgres.db.harshdev.io
KIND: ManagedPostgres
...
```

**Verify:**
```powershell
kubectl get mpg -A
# No resources found — CRD exists, but no instances yet. This is normal.
```

**Why This Matters:**
`kubectl explain` works because the API server serves your CRD schema. This is the same mechanism Deployments use. Senior pattern: validation happens in the API server, not your Python code — bad YAML never reaches your operator.

**Experiment:** Open `crds/managedpostgres.yaml`. Find `subresources: status: {}`. That one block prevents infinite reconcile loops (see Module 2).

---

### Lab 1.2: Validation Rejects Bad Input

**Concept:** Admission-time validation.
**Time:** 10 min

```powershell
# This file contains 5 intentionally broken resources
kubectl apply -f examples/manifests-invalid.yaml
```

**Expected Output:**
```text
error: ... storage in body should match '^...Mi|Gi...': "10"
error: ... version: Unsupported value: "13": supported values: "14", "15", "16"
...
```

Each object is rejected **individually** with a reason pointing at the exact field.

**Verify:**
```powershell
kubectl get mpg
# No resources found — nothing was persisted. Validation is atomic per object.
```

**Why This Matters:**
In production, this is your first line of defense. A junior operator validates in Python (`if version not in [...]`). A senior operator validates in the CRD so `kubectl`, Helm, ArgoCD, and every client gets the same error for free.

**Experiment:** Change `storage: "10"` to `storage: "10Gi"` in one doc and re-apply. That one succeeds, others still fail.

---

### Lab 1.3: Create a Valid Instance (No Operator Running Yet)

**Concept:** Desired state vs actual state.
**Time:** 10 min

```powershell
kubectl apply -f examples/manifests.yaml
kubectl get mpg
kubectl get mpg my-app-db -o yaml
kubectl describe mpg my-app-db
```

**Expected Output:**
```text
managedpostgres.db.harshdev.io/my-app-db created
NAME         AGE
my-app-db    5s
staging-db   5s
prod-db      5s
```

`status:` will be empty or minimal. No Pods, PVCs, Secrets are created.

**Verify:**
```powershell
kubectl get pods,sts,pvc,secret -l app.kubernetes.io/managed-by=postgres-operator
# No resources found — proves CRDs do nothing by themselves.
```

**Why This Matters:**
This is the core K8s split: **etcd stores intent, controllers create reality**. CRD = new intent type. Operator = the controller that makes reality match. Right now you have intent with no controller — Module 2 fixes that.

---

### Lab 1.4 — PROBLEM: What Happens When You Delete a CRD?

**Concept:** CRD deletion cascades to all custom resources.
**Time:** 10 min

> ⚠️ Do this in your dev cluster only.

```powershell
# Check what you have
kubectl get mpg

# Delete the CRD definition itself
kubectl delete crd managedpostgres.db.harshdev.io

# Try to list instances
kubectl get mpg
```

**Expected Output:**
```text
error: the server doesn't have a resource type "managedpostgres"
```

All `my-app-db`, `staging-db`, `prod-db` objects vanish from etcd instantly — **without running your delete handler, without backup, without finalizer**.

**Recover:**
```powershell
kubectl apply -f crds/
kubectl apply -f examples/manifests.yaml
kubectl get mpg
```

**Why This Matters:**
This is why production operators guard CRDs with Helm `keep` annotations and never delete them on uninstall. It's also why real DB operators use **finalizers** (Module 8) — to take a final backup before allowing deletion. CRD deletion bypasses finalizers entirely; nothing can stop it.

---

## Module 2: Operator Pattern & Reconciliation

**Goal:** Understand the control loop: watch → diff → act → update status.

Architecture recap:
`kopf` opens a watch stream to the API server. On ADDED/MODIFIED/DELETED it calls your handler in `operator/main.py`. Every 60s a timer calls `reconcile()` even if nothing changed (drift detection). All K8s writes go through `operator/provisioner.py` which is idempotent.

### Lab 2.1: Run the Operator Locally

**Concept:** Out-of-cluster operator development.
**Time:** 15 min

```powershell
# Terminal 1 — from repo root
pip install -r requirements.txt
kubectl apply -f helm/postgres-operator/templates/rbac.yaml
cd operator
python main.py
```

**Expected Output:**
```text
Using local kubeconfig (~/.kube/config)
Postgres Operator started
Watching: db.harshdev.io/v1 ManagedPostgres
Prometheus metrics available at :8080/metrics
```

Leave this terminal running. All future labs tail this log.

**Verify (Terminal 2):**
```powershell
curl http://localhost:8080/metrics | Select-String "postgres_operator"
# You should see HELP + TYPE lines for instances_total, reconcile_total, etc.
```

**Why This Matters:**
Running out-of-cluster with `~/.kube/config` is the fastest dev loop — no image build, no pod restart. In-cluster it uses `load_incluster_config()` + ServiceAccount token. Same code, different auth. This is standard for kopf/controller-runtime dev.

**Troubleshooting:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ConfigException` | kubeconfig missing | `kubectl config view`, check `~/.kube/config` exists |
| `403 Forbidden` | RBAC not applied | Re-run `kubectl apply -f helm/.../rbac.yaml` |
| Port 8080 in use | Old operator still running | Kill old `python main.py` process |

---

### Lab 2.2: Trace a CREATE — Secret → PVC → StatefulSet → Service

**Concept:** Event-driven provisioning.
**Time:** 15 min

```powershell
# Terminal 2 — delete and recreate to see a clean CREATE
kubectl delete mpg --all
kubectl apply -f examples/manifests.yaml
```

Watch **Terminal 1 (operator log)**:

**Expected Output:**
```text
CREATE: default/my-app-db
[my-app-db] Secret: created
[my-app-db] PVC: created
[my-app-db] StatefulSet: created
[my-app-db] Service: created
[my-app-db] CREATE complete in 2.34s → my-app-db.default.svc.cluster.local:5432
```

**Verify:**
```powershell
kubectl get secret,pvc,sts,svc -l app.kubernetes.io/instance=my-app-db
kubectl describe mpg my-app-db | Select-String "Phase|Endpoint|Message" -Context 0,2
# Phase: Running, Endpoint: my-app-db.default.svc.cluster.local:5432
```

**Why This Matters:**
Order matters: Secret first (StatefulSet mounts it), PVC before StatefulSet (pod mounts volume), Service last (selects ready pods). If the operator crashes after Secret but before StatefulSet, the next CREATE must not fail on "Secret already exists" — that's idempotency, implemented in `provisioner.py:resource_exists()`.

**Experiment:** In `operator/provisioner.py`, read `ensure_secret()` lines ~204-268. Note: on update it patches **metadata only, never `data`**. Regenerating a password on every reconcile would break all connections — a classic junior bug this code avoids.

---

### Lab 2.3: Labels, Selectors, OwnerReferences

**Concept:** How K8s resources find and own each other.
**Time:** 10 min

```powershell
kubectl get sts my-app-db -o yaml | Select-String "ownerReferences" -Context 0,8
kubectl get secret my-app-db-credentials -o yaml | Select-String "ownerReferences" -Context 0,8

kubectl get pods -l app.kubernetes.io/instance=my-app-db --show-labels
kubectl get svc my-app-db -o yaml | Select-String "selector" -Context 0,4
```

**Expected Output:**
OwnerReference points to `kind: ManagedPostgres, name: my-app-db, uid: <uid>, controller: true, blockOwnerDeletion: true`. Service selector matches pod labels `app.kubernetes.io/instance: my-app-db`.

**Why This Matters:**
Three different linking mechanisms:
1. **Labels + selectors** (Service → Pods): loose coupling, traffic routing.
2. **OwnerReferences** (ManagedPostgres → all children): garbage collection. Delete the CR → K8s deletes everything.
3. **Name convention** (`<name>-credentials`, `<name>-data`): how operator finds children without listing.

**Experiment:**
```powershell
kubectl delete mpg my-app-db
kubectl get sts,secret,pvc,svc -l app.kubernetes.io/instance=my-app-db
# All gone — K8s GC did it, not your delete handler. Check main.py:on_delete — it only updates metrics.
kubectl apply -f examples/manifests.yaml  # recreate for next labs
```

---

### Lab 2.4: UPDATE — Hash-Based Change Detection

**Concept:** Avoid unnecessary API writes.
**Time:** 15 min

```powershell
# Change storage on staging-db from 10Gi to 15Gi
kubectl patch mpg staging-db --type=merge -p '{"spec":{"storage":"15Gi"}}'
```

Watch operator log. Then:

```powershell
kubectl get pvc staging-db-data -o yaml | Select-String "storage"
kubectl get sts staging-db -o yaml | Select-String "last-applied-hash"
```

**Expected:** `ensure_pvc()` detects `current_hash != stored_hash` → patches PVC. If StorageClass allows expansion, PVC grows; else you see the warning from `provisioner.py:313-319` (`422 ... may not support expansion`) and it returns `unchanged`.

**Why This Matters:**
Naive operators `patch` everything on every event → API server hot-loop, `generation` churn, alert noise. This operator stores `db.harshdev.io/last-applied-hash` annotation (see `config.py:ANNOTATION_LAST_APPLIED`, `provisioner.py:spec_hash()`). No-op reconciles are pure GETs + string compare (<100ms). Check `reconcile_total{result="noop"}` in metrics — that's the proof.

**Experiment:** Patch a no-op (same value). Log shows `unchanged` for all resources, no API PATCH calls (`k8s_api_calls_total` doesn't increase).

---

### Lab 2.5 — PROBLEM: Operator Crashes Mid-Create

**Concept:** Idempotency / crash recovery.
**Time:** 15 min

```powershell
# Terminal 2: delete one instance
kubectl delete mpg my-app-db

# Terminal 1: restart operator RIGHT after re-applying (simulate crash)
kubectl apply -f examples/manifests.yaml
# Immediately Ctrl+C the operator, then restart: python main.py
```

**Expected Output:** On restart, kopf re-fires CREATE. Log shows mix of `created` (for resources missed before crash) and `unchanged` (for ones already created). **No `AlreadyExists` errors, no duplicates.**

**Why This Matters:**
This is the interview question: "What if your operator dies halfway?" Answer: `resource_exists()` GET-before-CREATE in every `ensure_*` makes handlers safe to replay. This is the reconciler pattern at its core. Read `provisioner.py:112-124`.

---

### Lab 2.6 — PROBLEM: Manual Drift (Self-Healing)

**Concept:** Timer-based reconciliation vs event-driven.
**Time:** 10 min (60s wait)

```powershell
kubectl delete sts my-app-db
kubectl get sts my-app-db
# NotFound — no event fires on ManagedPostgres itself!

# Wait 60 seconds, watch operator log
Start-Sleep 70
kubectl get sts my-app-db
# Back!
```

**Expected Log:**
```text
[my-app-db] Drift detected and healed: {'statefulset': 'created'}
```

**Why This Matters:**
Events only fire when `ManagedPostgres` changes. Manual `kubectl delete sts` bypasses that. The `@kopf.timer(interval=60.0)` in `main.py:reconcile()` re-runs all `ensure_*` every minute and recreates anything missing. That's self-healing. Without it, drift is permanent.

**Experiment:** Delete the Service instead. Same heal. Delete the Secret's **data** (password)? Operator patches metadata only — it deliberately does NOT restore the password (would break connections). Think about whether that's correct.

---

## Module 3: Stateful Workloads on K8s

**Goal:** Why StatefulSet, PVCs, and probes — not Deployments.

### Lab 3.1: StatefulSet Identity and DNS

**Concept:** Stable network identity.
**Time:** 10 min

```powershell
kubectl get sts my-app-db -o yaml | Select-String "serviceName|replicas|updateStrategy" -Context 0,3
kubectl get pods -l app.kubernetes.io/instance=my-app-db -o wide
kubectl exec -it my-app-db-0 -- hostname
nslookup my-app-db.default.svc.cluster.local
```

**Expected:** Pod is `my-app-db-0` (ordinal, stable). `serviceName: my-app-db` is required — StatefulSet needs a headless/governing Service for DNS. Update strategy is `RollingUpdate`.

**Why This Matters:**
Deployments give random pod names (`web-7d9f8c-abc12`) — fine for stateless. Postgres replication/failover needs to know "who is primary, who is replica-1". StatefulSet gives `postgres-0, postgres-1` + ordered startup/shutdown. Read `provisioner.py:348-361` comment block.

**Note:** This project mounts a pre-created PVC (`volumes[].persistentVolumeClaim`) rather than `volumeClaimTemplates`. Simpler for single-instance; `volumeClaimTemplates` is the pattern when you need one PVC per replica.

---

### Lab 3.2: PVC Binding and Storage

**Concept:** Dynamic provisioning via StorageClass.
**Time:** 10 min

```powershell
kubectl get pvc my-app-db-data
kubectl get pvc my-app-db-data -o yaml | Select-String "storageClassName|phase|capacity" -Context 0,2
kubectl get sc
kubectl get pv | Select-String "my-app-db"
```

**Expected:** PVC `Bound`, StorageClass = kind default (`standard` via `rancher.io/local-path` or `local-path`). PV auto-provisioned.

**Why This Matters:**
`accessModes: ["ReadWriteOnce"]` = one node at a time. Correct for a single Postgres primary. `storage: "5Gi"` is **mostly immutable** — you can grow (if `allowVolumeExpansion: true`) but never shrink. That's why `ensure_pvc()` only patches growth and catches 422.

**Experiment:**
```powershell
kubectl get sc standard -o yaml | Select-String "allowVolumeExpansion"
# kind default is usually true — so Lab 2.4 growth should have worked.
```

---

### Lab 3.3: Probes — liveness vs readiness

**Concept:** Restart vs traffic removal.
**Time:** 10 min

```powershell
kubectl get sts my-app-db -o yaml | Select-String "livenessProbe|readinessProbe|pg_isready" -Context 0,6
kubectl describe pod my-app-db-0 | Select-String "Liveness|Readiness|Restart" -Context 0,3
```

**Expected:** Both probes run `pg_isready -U postgres`. Liveness: `initialDelaySeconds: 30, periodSeconds: 10, failureThreshold: 6` (~60s before restart). Readiness: `initialDelaySeconds: 5, periodSeconds: 5, failureThreshold: 3`.

**Why This Matters:**
- **Liveness fails** → kubelet restarts the container. Too aggressive = crash loop during slow startup (hence 30s delay).
- **Readiness fails** → pod removed from Service endpoints, no traffic, **no restart**. During a slow query spike you want to shed traffic, not restart the DB.

**Experiment:** `kubectl exec my-app-db-0 -- pg_ctl stop -m fast` (or kill postgres). Watch `kubectl get pods -w` — liveness fails 6x → `Restart Count` increments.

---

### Lab 3.4 — PROBLEM: PVC Full

**Concept:** Storage exhaustion is a DB killer.
**Time:** 10 min

```powershell
kubectl exec -it my-app-db-0 -- df -h /var/lib/postgresql/data
kubectl exec -it my-app-db-0 -- dd if=/dev/zero of=/var/lib/postgresql/data/fill.test bs=1M count=100
kubectl get events --sort-by=.metadata.creationTimestamp | Select-Object -Last 10
rm # (delete the fill file after observing)
kubectl exec -it my-app-db-0 -- rm /var/lib/postgresql/data/fill.test
```

**Why This Matters:**
No alert in this project watches disk by default — but `prometheus/alerting-rules.yaml` has `PostgresPVCUsageHigh` (>85%) using `kubelet_volume_stats_used_bytes`. In Module 7 you'll see it fire. Production lesson: disk-full on Postgres = WAL cannot write = hard down. Monitor it, alert it, auto-expand or vacuum.

---

## Module 4: Backup Architecture

**Goal:** CronJob-driven `pg_dump` with pluggable backends.

Flow: `ManagedPostgres(backupEnabled:true)` → operator creates CronJob → CronJob spawns backup container → `backup/backup.py` runs `pg_dump | gzip` → uploads to local/S3 → enforces retention → exit 0/1.

### Lab 4.1: Enable Backup, Inspect CronJob

**Concept:** CronJob spec.
**Time:** 10 min

```powershell
kubectl apply -f examples/manifests-backup.yaml
kubectl get cronjob
kubectl get cronjob app-with-backup-backup -o yaml | Select-String "schedule|concurrencyPolicy|successfulJobs|failedJobs|backoffLimit" -Context 0,2
```

**Expected:** `schedule: "0 2 * * *"`, `concurrencyPolicy: Forbid` (never overlap), `successfulJobsHistoryLimit: 3`, `failedJobsHistoryLimit: 3`, `backoffLimit: 2`.

**Why This Matters:**
`Forbid` prevents two dumps running concurrently (would double load + corrupt retention count). History limits keep last 3 Job pods for `kubectl logs` debugging without cluttering etcd. These three fields are what separate a demo CronJob from a production one.

---

### Lab 4.2: Trigger Manual Backup, Read Logs

**Concept:** `kubectl create job --from=cronjob`.
**Time:** 15 min

```powershell
kubectl create job --from=cronjob/app-with-backup-backup manual-backup-1
kubectl get jobs -w
kubectl logs job/manual-backup-1
```

**Expected Log (from `backup/backup.py`):**
```text
Starting backup: db=app-with-backup backend=local retention=7d
Running pg_dump: pg_dump -h ... -U postgres -d app-with-backup -F p --no-password
pg_dump complete: /tmp/...sql.gz (x.x MB)
Local backup saved: /backups/...
Retention: deleted 0 old backups, 1 remaining
Backup complete
```

**Verify:**
```powershell
kubectl get jobs manual-backup-1 -o yaml | Select-String "succeeded|failed"
# succeeded: 1
```

**Why This Matters:**
`pg_dump -F p` = plain SQL, most portable (restores with `psql`). Piped straight to `gzip` so uncompressed SQL never touches disk — matters at 100GB scale. `PGPASSWORD` env var avoids interactive prompt. Exit code drives CronJob retry.

---

### Lab 4.3: Secret Injection (No Hardcoded Creds)

**Concept:** EnvFrom Secret + SecretKeyRef.
**Time:** 10 min

```powershell
kubectl get cronjob app-with-backup-backup -o yaml | Select-String "POSTGRES_PASSWORD|secretKeyRef|POSTGRES_HOST" -Context 0,4
kubectl get secret app-with-backup-credentials -o yaml | Select-String "DATABASE_URL"
```

**Expected:** CronJob sets `POSTGRES_HOST=<svc-dns>` as plaintext (not sensitive) and `POSTGRES_PASSWORD` via `valueFrom.secretKeyRef`. Backup container never sees the password in the CR spec.

**Why This Matters:**
`kubectl get mpg -o yaml` shows no credentials. Secrets stay in Secret objects with RBAC. If you `kubectl describe cronjob`, the password value is hidden (`<set to the key ...>`). This is the correct K8s secrets pattern.

---

### Lab 4.4: Local vs S3 Backend

**Concept:** ABC pluggable backend.
**Time:** 10 min (read + compare, S3 needs real creds)

```powershell
# Read the factory
# backup/backup.py:get_backend() — one elif per backend, rest of script unchanged
# S3 path: s3://<bucket>/<namespace>/<db>/<timestamp>.sql.gz with AES256 SSE
```

To try S3 for real:
```powershell
kubectl create secret generic aws-backup-creds --from-literal=AWS_ACCESS_KEY_ID=test --from-literal=AWS_SECRET_ACCESS_KEY=test -n default
kubectl apply -f examples/manifests-s3.yaml
kubectl get cronjob app-with-s3-backup-backup -o yaml | Select-String "S3_BUCKET|S3_REGION"
```

**Why This Matters:**
`BackupBackend` ABC (`upload`, `list_backups`, `delete`) means adding GCS/Azure = one class + one `elif`. `boto3` is imported lazily inside `S3Backend.__init__` so `local` mode doesn't need it. Retention parses timestamps from filenames — no external metadata DB.

**Experiment:** Read `apply_retention()` in `backup.py`. Filenames are `<db>-<YYYYMMDDTHHMMSSZ>.sql.gz`. Unparseable names are skipped with a warning, not deleted — safe default.

---

### Lab 4.5 — PROBLEM: Backup Fails, CronJob Retries

**Concept:** Failure handling via exit codes.
**Time:** 10 min

```powershell
# Break the backup by pointing at a non-existent host (edit a copy, don't commit)
kubectl patch cronjob app-with-backup-backup --type=json -p='[{"op":"replace","path":"/spec/jobTemplate/spec/template/spec/containers/0/env","value":[{"name":"DB_NAME","value":"nonexistent-db"},{"name":"POSTGRES_HOST","value":"invalid-host"},{"name":"BACKEND","value":"local"}]}]'
kubectl create job --from=cronjob/app-with-backup-backup broken-backup-1
kubectl logs job/broken-backup-1
kubectl get job broken-backup-1 -o yaml | Select-String "backoffLimit|failed"
```

**Expected:** `pg_dump failed (exit 1)`, container exits 1, Job retries per `backoffLimit: 2`, then marked `Failed`. `backups_failed_total` metric increments.

**Recover:**
```powershell
kubectl delete job broken-backup-1
# Re-apply to restore correct CronJob (operator will also heal it within 60s via reconcile)
kubectl apply -f examples/manifests-backup.yaml
```

**Why This Matters:**
`sys.exit(1)` is the entire error contract between backup script and K8s. No custom API, no webhook — Unix exit codes + `backoffLimit` + `failedJobsHistoryLimit` give you retries + debuggability for free.

---

## Module 5: Restore & Job Pattern

**Goal:** One-time Jobs with initContainers.

Restore flow: user creates `PostgresRestore` → `main.py:on_restore_create` validates → `restore.py:RestoreManager` creates Job → initContainer downloads backup to shared `emptyDir` → main container `gunzip -c | psql` → timer `restore_reconcile` polls Job → updates CR `Pending → Running → Completed/Failed`.

### Lab 5.1: Create a Restore

**Concept:** Async operation via custom resource.
**Time:** 15 min

```powershell
# Use a real backup filename from Lab 4.2 logs, or list local backups:
kubectl logs job/manual-backup-1 | Select-String "Backup stored|complete"

# Edit examples/restore.yaml to match your filename + target DB, then:
kubectl apply -f examples/restore.yaml
kubectl get pgr
kubectl describe pgr restore-staging-to-dev | Select-String "Phase|Message|JobName" -Context 0,2
```

**Expected:** Phase `Pending` → `Running` (Job created), `jobName: restore-staging-to-dev-restore`.

**Verify:**
```powershell
kubectl get jobs | Select-String "restore"
kubectl get job restore-staging-to-dev-restore -o yaml | Select-String "initContainers|restore|psql" -Context 0,3
```

**Why This Matters:**
CronJob = repeating schedule. Job = run once to completion. Restore is one-shot, so Job with `backoffLimit: 0` (no blind retry — you want manual retry after inspecting failure) + `ttlSecondsAfterFinished: 3600` (auto-cleanup after 1h).

---

### Lab 5.2: initContainer + Shared Volume

**Concept:** Separation of download vs restore.
**Time:** 10 min

```powershell
kubectl get job restore-staging-to-dev-restore -o yaml | Select-String "initContainers:" -Context 0,15
kubectl logs job/restore-staging-to-dev-restore -c download-backup
kubectl logs job/restore-staging-to-dev-restore -c restore
```

**Expected:** Two containers, one shared `emptyDir` volume `backup-data` mounted at `/backup`. S3 variant uses `amazon/aws-cli` image; local variant uses `busybox` `cp`. Main uses `postgres:15-alpine` and runs `until pg_isready ...; gunzip -c ... | psql ... -v ON_ERROR_STOP=1`.

**Why This Matters:**
If download fails, Job fails **before touching the DB**. Main container stays simple (just `psql`). `-v ON_ERROR_STOP=1` aborts on first SQL error instead of half-restoring. `pg_isready` wait loop handles the case where Postgres isn't up yet.

---

### Lab 5.3: Poll Status to Completion

**Concept:** Timer-based status sync.
**Time:** 10 min

```powershell
kubectl get pgr restore-staging-to-dev -w
# Wait for Completed. Then:
kubectl describe pgr restore-staging-to-dev | Select-String "Phase|CompletionTime"
```

The `restore_reconcile` timer (every 30s) calls `get_job_status()` and patches the CR. Terminal states (`Completed`/`Failed`) stop further updates.

**Why This Matters:**
Jobs don't update your CR — your operator must poll and reflect. This is the standard pattern for wrapping any async K8s resource (Jobs, PVC binding, LoadBalancer provisioning).

---

### Labs 5.4–5.5 — PROBLEMS

**5.4: Restore to non-existent DB.**
```powershell
kubectl apply -f - --dry-run=client -o yaml <<EOF
# (PowerShell: use a file instead of heredoc)
EOF
```
Create a `PostgresRestore` with `targetDatabase: does-not-exist`. The Job's `psql` fails `pg_isready`, Job fails, CR goes `Failed` with message. Lesson: validate target exists in `on_restore_create` (currently raises `PermanentError` only for missing fields — consider adding a ManagedPostgres existence check as an exercise).

**5.5: Wrong backup filename.** Same flow — initContainer `cp`/`aws s3 cp` fails, Job fails fast, DB untouched. That's the initContainer payoff.

---

## Module 6: Helm Packaging

**Goal:** Template, install, upgrade, distribute.

Your chart: `helm/postgres-operator/` (Chart.yaml, values.yaml, templates/deployment.yaml, servicemonitor.yaml, rbac.yaml, _helpers.tpl).

### Lab 6.1: Render Without Installing

**Concept:** `helm template` + values substitution.
**Time:** 10 min

```powershell
helm lint ./helm/postgres-operator
helm template pg-op ./helm/postgres-operator --namespace default
helm template pg-op ./helm/postgres-operator --set image.tag=v9.9.9 --set replicaCount=3 | Select-String "image:|replicas:"
```

**Expected:** Full manifests printed, your overrides reflected. No cluster changes.

**Why This Matters:**
`helm template` is your fastest feedback loop for chart bugs — no install needed. `helm lint` catches schema errors. Senior habit: template + diff before every upgrade.

---

### Lab 6.2: Install and Inspect Release

**Concept:** Release = chart + values + revision.
**Time:** 10 min

```powershell
helm install pg-op ./helm/postgres-operator --namespace default --create-namespace --wait --timeout=5m
helm list -A
helm status pg-op -n default
kubectl get deploy pg-op-postgres-operator -o yaml | Select-String "image:|METRICS_PORT|LOG_LEVEL" -Context 0,2
```

**Expected:** Deployment with 1 replica, env `OPERATOR_NAMESPACE` from fieldRef, `LOG_LEVEL` from values, probes hitting `/metrics`, annotations `prometheus.io/scrape: "true"`.

**Why This Matters:**
Deployment (not bare Pod) gives rolling updates + self-healing for the operator itself. Probes on `/metrics` reuse the Prometheus endpoint as a health check — no extra server needed. `serviceAccountName` binds the RBAC from Lab 2.1.

---

### Lab 6.3: Upgrade With New Values

**Concept:** Declarative upgrades, revision history.
**Time:** 10 min

```powershell
helm upgrade pg-op ./helm/postgres-operator -n default --set logLevel=DEBUG --set metrics.port=8080
helm history pg-op -n default
kubectl rollout status deploy/pg-op-postgres-operator -n default
helm rollback pg-op 1 -n default
```

**Expected:** Revision 1 → 2 → rollback to 1. Pod rolls with new env.

**Why This Matters:**
Helm tracks every upgrade as a revision with the exact values used. `rollback` is instant disaster recovery for bad config. In GitOps (ArgoCD), values live in git — same mechanism, different driver.

---

### Lab 6.4: Helpers and Naming

**Concept:** `_helpers.tpl` DRY templates.
**Time:** 10 min

Open `helm/postgres-operator/templates/_helpers.tpl`. Find `postgres-operator.fullname`, `postgres-operator.labels`, `postgres-operator.serviceAccountName`.

```powershell
helm template pg-op ./helm/postgres-operator --set fullnameOverride=custom-name | Select-String "name: custom-name" | Select-Object -First 5
helm template pg-op ./helm/postgres-operator --set nameOverride=x | Select-String "app.kubernetes.io/name"
```

**Why This Matters:**
Every resource (Deployment, ServiceMonitor, ServiceAccount) uses the same helpers → labels always match selectors. Mismatched selector/label is the #1 Helm bug; helpers eliminate it. `trunc 63 | trimSuffix "-"` respects K8s DNS-1123 name limits.

---

### Labs 6.5–6.6 — PROBLEM + Package

**6.5: Break the template.** Add `{{ .Values.nonexistent }}` to `deployment.yaml`, run `helm template`. Helm renders empty string (Go templates are lenient) — observe silent failure. Fix with `required`: `{{ required "image.repository is required" .Values.image.repository }}`. Lesson: fail fast with clear errors.

**6.6: Package.**
```powershell
helm package ./helm/postgres-operator
# Produces postgres-operator-1.0.0.tgz — the distributable artifact (like a Docker image for K8s YAML).
```

---

## Module 7: Observability Stack

**Goal:** Metrics → Prometheus → Grafana → Alerts.

### Lab 7.1: Read Raw Metrics

**Concept:** Counter vs Gauge vs Histogram.
**Time:** 10 min

```powershell
curl http://localhost:8080/metrics | Select-String "postgres_operator"
```

Identify:
- `instances_total` = **Gauge** (up/down, labels `namespace,phase`)
- `reconcile_total` = **Counter** (only up, labels `namespace,result`)
- `reconcile_duration_seconds` = **Histogram** (buckets, computes p50/p95/p99)

**Why This Matters:**
Choosing the wrong type is a permanent schema mistake (can't convert Counter→Gauge later without breaking dashboards). Rule: counts that go down = Gauge; totals = Counter; latencies/sizes = Histogram. See `operator/metrics.py` grouping.

---

### Lab 7.2: Prometheus + ServiceMonitor Discovery

**Concept:** Label-based scrape discovery.
**Time:** 15 min

```powershell
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack --namespace monitoring --create-namespace --wait
kubectl get servicemonitor -n default
kubectl get servicemonitor pg-op-postgres-operator -n default -o yaml | Select-String "selector|port:|interval" -Context 0,3
```

**Expected:** ServiceMonitor selector matches Deployment labels (`app.kubernetes.io/name,instance`). Prometheus Operator sees the ServiceMonitor and starts scraping `:8080/metrics` every 30s. No static config.

**Why This Matters:**
Static `prometheus.yml` (in `prometheus/` dir) lists targets by hand — breaks when pods move. ServiceMonitor = "scrape anything with these labels". This is how you get zero-config monitoring for every new operator release. The `prometheus.io/scrape` annotations on the pod template are the fallback for non-Operator Prometheus.

---

### Lab 7.3: PromQL Basics

**Concept:** Query your operator.
**Time:** 10 min

Port-forward Prometheus, open UI, try:
```promql
postgres_operator_instances_total
postgres_operator_instances_total{phase="Running"}
rate(postgres_operator_reconcile_total{result="healed"}[5m])
histogram_quantile(0.95, rate(postgres_operator_reconcile_duration_seconds_bucket[5m]))
time() - postgres_operator_last_backup_timestamp_seconds
```

**Why This Matters:**
- `rate(...[5m])` turns counters into per-second rates (alert on symptoms, not totals).
- `histogram_quantile` gives SLO-grade latency (p95 < 10s).
- `time() - last_backup` = staleness detector (basis for `PostgresBackupStale` alert).

---

### Lab 7.4: Grafana Dashboard

**Concept:** Dashboard-as-code.
**Time:** 10 min

Import `grafana/dashboard.json` into Grafana (Dashboards → Import). Explore: instance-by-phase stat, reconcile p50/p95/p99, backup success/failure, time-since-last-backup, PVC usage.

**Why This Matters:**
JSON on disk = versioned, reviewable, restorable. Templating variables (`namespace`, `instance`) make one dashboard work for all DBs. The 17 panels map 1:1 to the metrics in `metrics.py` + kube-state-metrics.

---

### Labs 7.5–7.6 — PROBLEMS (Alerts)

**7.5: Kill the operator.** `kubectl delete pod -l app.kubernetes.io/name=postgres-operator`. Within 2 min, `PostgresOperatorDown` fires (`absent(up{job="postgres-operator"} == 1)`). Deployment recreates the pod; alert resolves. Lesson: alert on `absent()`, not just `== 0` (handles never-existed vs crashed).

**7.6: Stale backup.** `time() - last_backup > 25h` fires if CronJob is suspended: `kubectl patch cronjob app-with-backup-backup -p '{"spec":{"suspend":true}}'`, wait, observe. Unsuspend after. Lesson: staleness alerts catch silent CronJob failures that error-rate alerts miss (no runs = no errors).

---

## Module 8: Production Hardening

**Goal:** What separates demo from production. Each lab is a gap in the current code + how to close it.

### Lab 8.1: Audit RBAC (Least Privilege)

```powershell
kubectl get clusterrole pg-op-postgres-operator -o yaml | Select-String "resources|verbs" -Context 0,6
```

Verify it has only `get/list/watch/create/patch/delete` on `statefulsets, secrets, pvcs, services, cronjobs, jobs, managedpostgres, postgresrestores` + `update/status` on the CRs. No `*`, no `nodes`, no `cluster-admin`.

**Why:** A compromised operator with cluster-admin = full cluster takeover. Least privilege bounds blast radius.

### Lab 8.2 — PROBLEM: Two Operators Fight (Leader Election)

Scale the Deployment to 2. Both watch the same CRs, both PATCH status, both create Jobs → conflicts (`409`), duplicate metrics, flapping.

```powershell
kubectl scale deploy pg-op-postgres-operator --replicas=2
kubectl logs -l app.kubernetes.io/name=postgres-operator --tail=20 | Select-String "Conflict|409|already exists"
```

Fix (exercise): enable kopf peering (`kopf.run(..., peering_name=...)` with Lease) or controller-runtime leader election. Only the leader reconciles; standby watches.

### Lab 8.3: TLS for Postgres

Current `DATABASE_URL` is `postgresql://` plaintext. Exercise: add `tls` to CRD spec, mount certs via cert-manager `Certificate`, set `ssl=on` in StatefulSet, change URL to `postgresql://...?sslmode=require`. Verify with `psql "sslmode=require"`.

### Lab 8.4: Connection Pooling (PgBouncer Sidecar)

Direct Postgres connections don't scale (each = process fork). Exercise: add PgBouncer sidecar container in `ensure_statefulset()`, apps connect to `:6432`, PgBouncer pools to `:5432`. Observe `pg_stat_activity` drop.

### Lab 8.5 — PROBLEM: Operator OOM

Remove memory limits, trigger 50 rapid spec updates, watch `kubectl top pod`. Fix: set `resources.limits.memory` (already in chart values), add pprof, batch status writes. Lesson: operators are API-server clients — unbounded watches + queues = OOM.

### Lab 8.6 — PROBLEM: Unsafe Delete (Finalizers) — IMPLEMENTED

The operator now registers `db.harshdev.io/finalizer` on every ManagedPostgres.
Test the behavior:

```powershell
kubectl delete mpg prod-db
# Watch: operator creates <name>-final-backup-<timestamp> Job (orphan, no ownerRef)
kubectl get jobs | Select-String "final-backup"
kubectl get pvc prod-db-backup-data
# Still there — the backup-data PVC is ownerless and survives CR deletion
kubectl get mpg prod-db
# NotFound — finalizer was removed, deletion completed
```

Key points: deletion is never blocked on backup success (a failed final backup is logged, scheduled backups in the surviving PVC remain). Re-apply the same CR and `autoRestore` brings the data back from `latest`. Note: deleting the **CRD itself** still wipes everything instantly and bypasses finalizers — never delete CRDs in a live cluster.

---

## Capstone: Production Incident (60 min)

**Scenario:** Friday 5pm. On-call ping: "prod-db is down."

```powershell
# Setup: fresh state
helm upgrade --install pg-op ./helm/postgres-operator -n default --wait
kubectl apply -f examples/manifests.yaml
kubectl apply -f examples/manifests-backup.yaml

# 1. INCIDENT: someone deletes the prod StatefulSet
kubectl delete sts prod-db

# 2. DETECT: check operator logs + metrics
# Expect: "Drift detected and healed" within 60s
kubectl logs -l app.kubernetes.io/name=postgres-operator --tail=20
curl -s http://localhost:8080/metrics | Select-String 'reconcile_total.*healed'

# 3. DATA LOSS: PVC was also deleted (simulate corruption)
kubectl delete pvc prod-db-data
# Operator recreates empty PVC — then autoRestore kicks in (data-pvc-recreated):
# it auto-creates a PostgresRestore with backupFile "latest".
kubectl get pgr -w
# Watch: <name>-auto-<timestamp> goes Pending → Running → Completed. No manual step.

# 4. RECOVER (manual fallback — only needed if autoRestore is disabled):
kubectl logs job/manual-backup-1 | Select-String "Backup stored"
# Edit examples/restore.yaml with real filename, targetDatabase: prod-db
kubectl apply -f examples/restore.yaml
kubectl get pgr -w
# Wait for Completed

# 5. VERIFY: connect, check data, check dashboards
kubectl port-forward svc/prod-db 5432:5432 &
# psql -h localhost -U postgres -d prod-db -c "SELECT count(*) FROM <your_table>;"
# Grafana: instances green, restore_completed_total incremented, no firing alerts
# Prometheus: histogram_quantile restore duration reasonable

# 6. POSTMORTEM: write 3 bullets
# - What healed automatically vs needed manual restore?
# - Which alert fired first? Which should have fired sooner?
# - What finalizer/backup policy would have prevented data loss?
```

**Outcome:** You prove the full loop — provision → monitor → fail → self-heal infra → restore data → verify observability. This is the demo that gets operator projects approved.

---

## Quick Reference

```powershell
# Cluster
kind get clusters
kubectl config use-context kind-postgres-dev

# Operator dev loop
pip install -r requirements.txt
cd operator; python main.py

# Daily commands
kubectl get mpg,pgr -A
kubectl describe mpg my-app-db
kubectl logs -l app.kubernetes.io/name=postgres-operator -f --tail=100
curl http://localhost:8080/metrics

# Helm
helm lint ./helm/postgres-operator
helm upgrade --install pg-op ./helm/postgres-operator -n default --wait
helm history pg-op -n default

# Backup / Restore
kubectl create job --from=cronjob/app-with-backup-backup manual-backup-1
kubectl logs job/manual-backup-1
kubectl apply -f examples/restore.yaml

# Cleanup
kubectl delete mpg,pgr --all
helm uninstall pg-op -n default
kind delete cluster --name postgres-dev
```

## Docs Map

| File | Use it for |
|------|------------|
| `README.md` | Install, deploy, spec reference, troubleshooting |
| `PROJECT_MAP.md` | File tree, what lives where |
| `LEARNING_GUIDE.md` | Concepts, patterns, why-each-file-exists |
| `WORKSHOP.md` (this file) | Step-by-step labs with outcomes |
