"""
main.py — The operator entrypoint. All kopf event handlers live here.

HOW kopf WORKS:
  kopf.run() connects to the K8s API server and opens a watch stream.
  Think of it like a websocket that says:
    "send me an event whenever a ManagedPostgres object changes"

  K8s sends events of three types:
    ADDED   → resource was created  → @kopf.on.create fires
    MODIFIED → resource was updated → @kopf.on.update fires
    DELETED → resource was deleted  → @kopf.on.delete fires

  kopf also has @kopf.timer which fires on an interval regardless
  of events. We use this for the reconciliation loop — even if no
  event fires, we periodically check that all resources are healthy.

HANDLER ARGUMENTS (kopf injects these automatically):
  spec      → the .spec block from the YAML the user applied
  status    → the current .status block
  meta      → metadata (name, namespace, uid, generation, annotations)
  patch     → a dict-like object — writes to it are sent to K8s at handler end
  logger    → structured logger scoped to this handler invocation
  name      → shortcut for meta["name"]
  namespace → shortcut for meta["namespace"]

ERROR HANDLING STRATEGY:
  kopf.TemporaryError → retry after delay (network blip, API server busy)
  kopf.PermanentError → don't retry (bad spec, unrecoverable state)
  Any other exception → kopf retries with exponential backoff by default

  We catch specific exceptions and convert them to the right type.
  This is what "failure handling" means at the operator level.
"""

import logging
import time

import kopf
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from config import (
    API_GROUP, API_VERSION,
    PHASE_RUNNING, PHASE_DEGRADED,
    CONDITION_DATABASE_READY, CONDITION_BACKUP_CONFIGURED, CONDITION_STORAGE_READY,
    RETRY_INITIAL_DELAY,
)
from metrics import METRICS
from provisioner import Provisioner
from status import StatusManager

logger = logging.getLogger(__name__)

# ── Operator startup ──────────────────────────────────────────────────────────

@kopf.on.startup()
def on_startup(settings: kopf.OperatorSettings, **kwargs):
    """
    Runs once when the operator starts.

    We configure kopf's retry behavior here and start the metrics server.
    kopf settings control how it handles errors, retries, and timeouts.
    """
    # How long kopf waits before retrying a failed handler
    settings.posting.level = logging.WARNING   # only post WARNING+ to K8s events
    settings.watching.connect_timeout = 30
    settings.watching.server_timeout  = 300    # re-establish watch every 5 min

    # Start Prometheus metrics server on :8080/metrics
    from metrics import METRICS
    METRICS.start_metrics_server()

    logger.info("Postgres Operator started")
    logger.info(f"Watching: {API_GROUP}/{API_VERSION} ManagedPostgres")


# ── CREATE handler ────────────────────────────────────────────────────────────

@kopf.on.create(API_GROUP, API_VERSION, "managedpostgres")
def on_create(spec, meta, status, patch, **kwargs):
    """
    Fires when a new ManagedPostgres resource is applied to the cluster.

    WHAT WE DO:
      1. Mark status as Creating
      2. Create all K8s resources via Provisioner (idempotent)
      3. Mark status as Running with the endpoint
      4. Increment metrics

    IDEMPOTENCY:
      This handler can be called multiple times for the same resource
      (operator restart, network partition, etc). Provisioner.ensure_*
      methods are safe to call repeatedly — they check before creating.

    FAILURE HANDLING:
      ApiException 403 → operator missing RBAC permissions → PermanentError
        (retrying won't help, cluster admin needs to fix RBAC)
      ApiException 5xx → K8s API server error → TemporaryError
        (retry after delay, server should recover)
      Any other error  → TemporaryError (let kopf retry with backoff)
    """
    name      = meta["name"]
    namespace = meta["namespace"]
    uid       = meta["uid"]
    api_ver   = f"{API_GROUP}/{API_VERSION}"

    logger.info(f"CREATE: {namespace}/{name}")
    METRICS.instances_created.labels(namespace=namespace).inc()

    sm = StatusManager(namespace, name, patch, status)
    sm.mark_creating()

    start = time.monotonic()

    try:
        p = Provisioner(namespace, name, uid, spec, api_ver)

        # Each ensure_* call is idempotent: create if missing, patch if changed
        action, _ = p.ensure_secret()
        logger.info(f"[{name}] Secret: {action}")
        sm.set_condition(CONDITION_STORAGE_READY, "False", "PVCPending", "Creating PVC")

        action, _ = p.ensure_pvc()
        logger.info(f"[{name}] PVC: {action}")
        sm.set_condition(CONDITION_STORAGE_READY, "True", "PVCCreated", "PVC created")

        action, _ = p.ensure_statefulset()
        logger.info(f"[{name}] StatefulSet: {action}")

        action, _ = p.ensure_service()
        logger.info(f"[{name}] Service: {action}")

        # Backup is optional — only configure if user enabled it
        if spec.get("backupEnabled", False):
            # TODO: replace with your actual backup image once built
            backup_image = "harshdev/postgres-backup:latest"
            action, _    = p.ensure_backup_cronjob(backup_image)
            logger.info(f"[{name}] CronJob: {action}")
            sm.set_condition(
                CONDITION_BACKUP_CONFIGURED, "True",
                "CronJobCreated",
                f"Backup scheduled: {spec.get('backupSchedule', '0 2 * * *')}"
            )
        else:
            sm.set_condition(
                CONDITION_BACKUP_CONFIGURED, "False",
                "BackupDisabled", "backupEnabled is false"
            )

        # Write final Running status
        sm.mark_running(p.endpoint())
        sm.set_observed_generation(meta.get("generation", 1))

        # Track how many instances are in Running state
        METRICS.instances_total.labels(
            namespace=namespace, phase=PHASE_RUNNING
        ).inc()

        duration = time.monotonic() - start
        METRICS.reconcile_total.labels(namespace=namespace, result="success").inc()
        METRICS.reconcile_duration.labels(namespace=namespace).observe(duration)

        logger.info(f"[{name}] CREATE complete in {duration:.2f}s → {p.endpoint()}")

    except ApiException as e:
        duration = time.monotonic() - start
        METRICS.reconcile_errors.labels(
            namespace=namespace, error_type=f"api_{e.status}"
        ).inc()

        if e.status == 403:
            # RBAC is wrong — retrying will never work
            sm.mark_degraded("InsufficientPermissions", str(e.reason))
            raise kopf.PermanentError(
                f"Operator lacks RBAC permissions: {e.reason}. "
                "Check ClusterRole and ClusterRoleBinding."
            )

        if e.status == 409:
            # Conflict — resource already exists but we missed it in exists check
            # This is a race condition — safe to retry
            raise kopf.TemporaryError(
                f"Conflict creating resource: {e.reason}", delay=RETRY_INITIAL_DELAY
            )

        # 5xx, 429 (rate limit), network errors — all retriable
        sm.mark_degraded("APIError", f"K8s API error {e.status}: {e.reason}")
        raise kopf.TemporaryError(
            f"K8s API error {e.status}: {e.reason}", delay=RETRY_INITIAL_DELAY
        )

    except Exception as e:
        METRICS.reconcile_errors.labels(
            namespace=namespace, error_type="unexpected"
        ).inc()
        sm.mark_degraded("UnexpectedError", str(e))
        logger.exception(f"[{name}] Unexpected error in CREATE handler")
        raise kopf.TemporaryError(str(e), delay=RETRY_INITIAL_DELAY)


# ── UPDATE handler ────────────────────────────────────────────────────────────

@kopf.on.update(API_GROUP, API_VERSION, "managedpostgres")
def on_update(spec, meta, status, patch, old, new, diff, **kwargs):
    """
    Fires when the user changes the ManagedPostgres spec.

    kopf passes `diff` — a list of changes between old and new spec.
    We use this to log what changed, but Provisioner's hash-based
    change detection handles the actual "do I need to update this resource" logic.

    `old` and `new` are the full old/new spec dicts.
    `diff` is a list of tuples: (operation, field_path, old_val, new_val)
      e.g. ('change', ('spec', 'storage'), '5Gi', '10Gi')

    WHAT CAN CHANGE:
      storage        → patch PVC (may fail if StorageClass doesn't support resize)
      backupSchedule → patch CronJob
      backupEnabled  → create or delete CronJob
      version        → patch StatefulSet image (triggers rolling update)
      resources      → patch StatefulSet resource requests/limits
    """
    name      = meta["name"]
    namespace = meta["namespace"]
    uid       = meta["uid"]
    api_ver   = f"{API_GROUP}/{API_VERSION}"

    # Log what actually changed — useful for debugging
    changes = [(op, "/".join(str(p) for p in path), ov, nv)
               for op, path, ov, nv in diff]
    logger.info(f"UPDATE: {namespace}/{name} — changes: {changes}")

    sm = StatusManager(namespace, name, patch, status)

    start = time.monotonic()

    try:
        p = Provisioner(namespace, name, uid, spec, api_ver)

        # Re-run all ensure_* — each one uses hash comparison to decide
        # whether to actually patch. If nothing changed for a resource, it's a noop.
        p.ensure_secret()
        p.ensure_pvc()
        p.ensure_statefulset()
        p.ensure_service()

        if spec.get("backupEnabled", False):
            backup_image = "harshdev/postgres-backup:latest"
            p.ensure_backup_cronjob(backup_image)
            sm.set_condition(
                CONDITION_BACKUP_CONFIGURED, "True",
                "CronJobUpdated",
                f"Backup schedule updated: {spec.get('backupSchedule')}"
            )

        sm.mark_running(p.endpoint())
        sm.set_observed_generation(meta.get("generation", 1))

        duration = time.monotonic() - start
        METRICS.reconcile_total.labels(namespace=namespace, result="success").inc()
        METRICS.reconcile_duration.labels(namespace=namespace).observe(duration)

        logger.info(f"[{name}] UPDATE complete in {duration:.2f}s")

    except ApiException as e:
        METRICS.reconcile_errors.labels(
            namespace=namespace, error_type=f"api_{e.status}"
        ).inc()
        sm.mark_degraded("UpdateFailed", f"API error {e.status}: {e.reason}")
        raise kopf.TemporaryError(str(e), delay=RETRY_INITIAL_DELAY)

    except Exception as e:
        METRICS.reconcile_errors.labels(
            namespace=namespace, error_type="unexpected"
        ).inc()
        sm.mark_degraded("UpdateFailed", str(e))
        raise kopf.TemporaryError(str(e), delay=RETRY_INITIAL_DELAY)


# ── DELETE handler ────────────────────────────────────────────────────────────

@kopf.on.delete(API_GROUP, API_VERSION, "managedpostgres")
def on_delete(spec, meta, status, patch, **kwargs):
    """
    Fires when a ManagedPostgres resource is deleted.

    IMPORTANT: We don't manually delete child resources here.
    Owner references handle that automatically — when ManagedPostgres
    is deleted, K8s garbage-collects all resources that have it as owner.

    What we DO here:
      1. Mark status as Terminating
      2. Log the deletion with relevant metadata
      3. Decrement running instance metrics

    If backups are enabled, a production operator would trigger a
    final backup here before teardown. We log a warning for now
    and will add that in the backup phase.
    """
    name      = meta["name"]
    namespace = meta["namespace"]

    logger.info(f"DELETE: {namespace}/{name}")

    sm = StatusManager(namespace, name, patch, status)
    sm.mark_terminating()

    if spec.get("backupEnabled", False):
        logger.warning(
            f"[{name}] Deletion triggered with backupEnabled=true. "
            "Final backup before teardown not yet implemented. "
            "Ensure you have a recent backup before deleting."
        )

    # Decrement the running instance gauge
    METRICS.instances_total.labels(namespace=namespace, phase=PHASE_RUNNING).dec()
    METRICS.instances_deleted.labels(namespace=namespace).inc()

    logger.info(
        f"[{name}] DELETE handler done. "
        "K8s will garbage-collect owned resources via owner references."
    )


# ── RECONCILIATION TIMER ──────────────────────────────────────────────────────

@kopf.timer(API_GROUP, API_VERSION, "managedpostgres", interval=60.0, sharp=True)
def reconcile(spec, meta, status, patch, **kwargs):
    """
    Runs every 60 seconds for every ManagedPostgres instance.

    WHY THIS EXISTS (the drift problem):
      Someone might manually delete the StatefulSet your operator created.
      Without a reconciliation loop, the operator would never notice —
      no event fires because ManagedPostgres itself wasn't changed.

      The timer fires every 60 seconds and re-runs all ensure_* checks.
      If the StatefulSet is missing, Provisioner recreates it.
      This is self-healing.

    `sharp=True` means the timer fires on exact intervals, not drifting.

    NOOP PATH:
      If everything is healthy, all ensure_* calls return "unchanged"
      and the handler completes in <100ms with no API writes.
      This is intentionally cheap — it's just GET calls + hash comparison.
    """
    name      = meta["name"]
    namespace = meta["namespace"]
    uid       = meta["uid"]
    api_ver   = f"{API_GROUP}/{API_VERSION}"

    # Skip reconciling if we're still in Creating phase
    current_phase = status.get("phase", "")
    if current_phase == "Creating":
        logger.debug(f"[{name}] Skipping reconcile — still Creating")
        return

    start = time.monotonic()

    try:
        p = Provisioner(namespace, name, uid, spec, api_ver)

        results = {
            "secret":      p.ensure_secret()[0],
            "pvc":         p.ensure_pvc()[0],
            "statefulset": p.ensure_statefulset()[0],
            "service":     p.ensure_service()[0],
        }

        if spec.get("backupEnabled", False):
            results["cronjob"] = p.ensure_backup_cronjob(
                "harshdev/postgres-backup:latest"
            )[0]

        # Only log if something was recreated (drift detected and healed)
        drifted = {k: v for k, v in results.items() if v != "unchanged"}
        if drifted:
            logger.warning(f"[{name}] Drift detected and healed: {drifted}")
            sm = StatusManager(namespace, name, patch, status)
            sm.mark_running(p.endpoint())

        duration = time.monotonic() - start
        result   = "healed" if drifted else "noop"
        METRICS.reconcile_total.labels(namespace=namespace, result=result).inc()
        METRICS.reconcile_duration.labels(namespace=namespace).observe(duration)

    except Exception as e:
        METRICS.reconcile_errors.labels(
            namespace=namespace, error_type="reconcile"
        ).inc()
        logger.error(f"[{name}] Reconcile error: {e}")
        # Don't raise — a failed timer tick shouldn't kill the operator


# ── RESTORE handler ───────────────────────────────────────────────────────────

@kopf.on.create(API_GROUP, API_VERSION, "postgresrestores")
def on_restore_create(spec, meta, status, patch, **kwargs):
    """
    Fires when a PostgresRestore resource is applied.

    Restore flow (to be fully implemented in backup phase):
      1. Find the target ManagedPostgres instance
      2. Download the specified backup file
      3. Spin up a restore Job
      4. Job runs: psql < backup.sql
      5. Update status when complete

    For now: validates input and marks as Pending.
    Full implementation comes in turn 3 (backup phase).
    """
    name            = meta["name"]
    namespace       = meta["namespace"]
    target_database = spec.get("targetDatabase")
    backup_file     = spec.get("backupFile")

    logger.info(f"RESTORE: {namespace}/{name} → {target_database} from {backup_file}")

    patch.status["phase"]   = "Pending"
    patch.status["message"] = (
        f"Restore from {backup_file} into {target_database} queued. "
        "Full restore implementation coming in backup phase."
    )
    patch.status["startTime"] = __import__("datetime").datetime.utcnow().isoformat()


# ── Operator entrypoint ───────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Run the operator.

    In-cluster: uses the ServiceAccount token mounted at
      /var/run/secrets/kubernetes.io/serviceaccount/token
    Out-of-cluster (local dev): uses ~/.kube/config

    kopf.run() blocks forever, processing events.
    """
    try:
        # Try in-cluster config first (running inside a pod)
        k8s_config.load_incluster_config()
        logger.info("Using in-cluster K8s config")
    except k8s_config.ConfigException:
        # Fall back to local kubeconfig (local dev with kind/minikube)
        k8s_config.load_kube_config()
        logger.info("Using local kubeconfig (~/.kube/config)")

    kopf.run(clusterwide=False)   # clusterwide=False = only watch current namespace
