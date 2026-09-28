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
from datetime import datetime, timezone

import kopf
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from config import (
    API_GROUP, API_VERSION,
    PHASE_RUNNING, PHASE_DEGRADED,
    CONDITION_DATABASE_READY, CONDITION_BACKUP_CONFIGURED, CONDITION_STORAGE_READY,
    RETRY_INITIAL_DELAY,
    FINALIZER,
    AUTO_RESTORE_LABEL, AUTO_RESTORE_DEDUP_MINUTES,
    backup_image_for_spec,
    LAST_PVC_UID_ANN,
)
from metrics import METRICS
from provisioner import Provisioner
from status import StatusManager
from restore import RestoreManager

logger = logging.getLogger(__name__)


def emit_event(namespace: str, name: str, event_type: str, reason: str, message: str):
    """
    Create a Kubernetes Event tied to the ManagedPostgres CR.
    This provides visible step-by-step feedback for demos/observability.
    """
    try:
        core_v1 = k8s_client.CoreV1Api()
        event = k8s_client.V1Event(
            metadata=k8s_client.V1ObjectMeta(
                generateName=f"{name}-",
                namespace=namespace,
            ),
            involved_object=k8s_client.V1ObjectReference(
                apiVersion=f"{API_GROUP}/{API_VERSION}",
                kind="ManagedPostgres",
                name=name,
                namespace=namespace,
            ),
            reason=reason,
            message=message,
            type=event_type,
            first_timestamp=datetime.now(timezone.utc).isoformat() + "Z",
            last_timestamp=datetime.now(timezone.utc).isoformat() + "Z",
            count=1,
        )
        core_v1.create_namespaced_event(namespace, event)
        logger.info(f"[{name}] Event emitted: {reason} — {message}")
    except Exception as e:
        logger.warning(f"[{name}] Failed to emit event {reason}: {e}")


# ── Finalizer + auto-restore helpers ──────────────────────────────────────────

def _custom_api() -> k8s_client.CustomObjectsApi:
    """CustomObjectsApi client (works in-cluster and with local kubeconfig)."""
    return k8s_client.CustomObjectsApi()


def get_managedpostgres(namespace: str, name: str) -> dict | None:
    """Fetch a ManagedPostgres CR. Returns None if it doesn't exist."""
    try:
        return _custom_api().get_namespaced_custom_object(
            group=API_GROUP, version=API_VERSION,
            namespace=namespace, plural="managedpostgres", name=name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def ensure_finalizer(namespace: str, name: str, finalizers: list | None):
    """
    Make sure our finalizer is present so delete always goes through on_delete
    (final backup). No-op if already present. Never blocks provisioning on error.
    """
    if finalizers and FINALIZER in finalizers:
        return
    try:
        _custom_api().patch_namespaced_custom_object(
            group=API_GROUP, version=API_VERSION,
            namespace=namespace, plural="managedpostgres", name=name,
            body={"metadata": {"finalizers": (finalizers or []) + [FINALIZER]}},
        )
        logger.info(f"[{name}] Finalizer added")
    except ApiException as e:
        logger.warning(f"[{name}] Could not add finalizer (non-fatal): {e.reason}")


def remove_finalizer(namespace: str, name: str):
    """Remove our finalizer so K8s can finish deletion. 404 = already gone."""
    try:
        obj = get_managedpostgres(namespace, name)
        if obj is None:
            return
        finalizers = (obj.get("metadata") or {}).get("finalizers") or []
        if FINALIZER not in finalizers:
            return
        remaining = [f for f in finalizers if f != FINALIZER]
        _custom_api().patch_namespaced_custom_object(
            group=API_GROUP, version=API_VERSION,
            namespace=namespace, plural="managedpostgres", name=name,
            body={"metadata": {"finalizers": remaining}},
        )
        logger.info(f"[{name}] Finalizer removed — deletion unblocked")
    except ApiException as e:
        if e.status != 404:
            logger.warning(f"[{name}] Could not remove finalizer: {e.reason}")


def maybe_auto_restore(namespace: str, name: str, spec: dict, reason: str):
    """
    Auto-create a PostgresRestore with backupFile "latest" after detected data
    loss. Deduplicates: skips if an auto-restore CR was created in the last
    AUTO_RESTORE_DEDUP_MINUTES. Best-effort — never raises.
    """
    if not spec.get("backupEnabled", False):
        return None
    if not spec.get("autoRestore", True):
        logger.debug(f"[{name}] autoRestore disabled — skipping ({reason})")
        return None

    try:
        api = _custom_api()
        existing = api.list_namespaced_custom_object(
            group=API_GROUP, version=API_VERSION,
            namespace=namespace, plural="postgresrestores",
            label_selector=f"{AUTO_RESTORE_LABEL}=true",
        )
        cutoff = time.time() - AUTO_RESTORE_DEDUP_MINUTES * 60
        for item in existing.get("items", []):
            item_spec = (item.get("spec") or {})
            if item_spec.get("targetDatabase") != name:
                continue
            ts = ((item.get("metadata") or {}).get("creationTimestamp") or "")
            try:
                created = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
            if created >= cutoff:
                logger.info(f"[{name}] Recent auto-restore exists — skipping ({reason})")
                return None

        restore_name = (
            f"{name}-auto-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        )[:253]
        body = {
            "apiVersion": f"{API_GROUP}/{API_VERSION}",
            "kind": "PostgresRestore",
            "metadata": {
                "name": restore_name,
                "namespace": namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "postgres-operator",
                    "app.kubernetes.io/instance": name,
                    AUTO_RESTORE_LABEL: "true",
                },
                "annotations": {
                    f"{API_GROUP}/auto-restore-reason": reason,
                },
            },
            "spec": {"targetDatabase": name, "backupFile": "latest"},
        }
        api.create_namespaced_custom_object(
            group=API_GROUP, version=API_VERSION,
            namespace=namespace, plural="postgresrestores", body=body,
        )
        logger.info(f"[{name}] Auto-restore triggered: {restore_name} ({reason})")
        return restore_name
    except ApiException as e:
        logger.warning(f"[{name}] Auto-restore failed (non-fatal): {e.reason}")
        return None

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

        # Register our finalizer first: guarantees on_delete runs (final backup)
        # even if provisioning below fails partway.
        ensure_finalizer(namespace, name, meta.get("finalizers"))

        # Each ensure_* call is idempotent: create if missing, patch if changed
        action, _ = p.ensure_secret()
        logger.info(f"[{name}] Secret: {action}")
        sm.set_condition(CONDITION_STORAGE_READY, "False", "PVCPending", "Creating PVC")

        action, pvc_result = p.ensure_pvc()
        logger.info(f"[{name}] PVC: {action}")
        sm.set_condition(CONDITION_STORAGE_READY, "True", "PVCCreated", "PVC created")
        # If PVC was created (not updated/unchanged), store its UID for data-loss detection
        if action == "created" and hasattr(pvc_result, 'metadata') and hasattr(pvc_result.metadata, 'uid'):
            pvc_uid = pvc_result.metadata.uid
        elif action == "created":
            # pvc_result is the UID string (new return format)
            pvc_uid = pvc_result
        else:
            pvc_uid = None
        
        if pvc_uid:
            # Store the PVC UID in the CR's metadata annotations via direct API patch
            try:
                api = _custom_api()
                api.patch_namespaced_custom_object(
                    group=API_GROUP, version=API_VERSION,
                    namespace=namespace, plural="managedpostgres", name=name,
                    body={"metadata": {"annotations": {LAST_PVC_UID_ANN: pvc_uid}}}
                )
                logger.info(f"[{name}] Stored PVC UID for data-loss detection: {pvc_uid}")
            except Exception as e:
                logger.warning(f"[{name}] Failed to store PVC UID annotation: {e}")

        action, _ = p.ensure_statefulset()
        logger.info(f"[{name}] StatefulSet: {action}")

        action, _ = p.ensure_service()
        logger.info(f"[{name}] Service: {action}")

        # Backup is optional — only configure if user enabled it
        if spec.get("backupEnabled", False):
            backup_image = backup_image_for_spec(spec)
            # Ownerless backup-data PVC first: CronJob mounts it at /backups.
            # "exists" means backups from a previous incarnation may be present.
            bp_action, _ = p.ensure_backup_data_pvc()
            logger.info(f"[{name}] BackupPVC: {bp_action}")
            action, _    = p.ensure_backup_cronjob(backup_image)
            logger.info(f"[{name}] CronJob: {action}")
            if bp_action == "exists":
                # Re-created instance, old backups survived → restore latest.
                maybe_auto_restore(namespace, name, spec, reason="recreated-with-backups")
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

        # Re-assert finalizer (covers objects created before finalizer support).
        ensure_finalizer(namespace, name, meta.get("finalizers"))

        # Re-run all ensure_* — each one uses hash comparison to decide
        # whether to actually patch. If nothing changed for a resource, it's a noop.
        p.ensure_secret()
        p.ensure_pvc()
        p.ensure_statefulset()
        p.ensure_service()

        if spec.get("backupEnabled", False):
            backup_image = backup_image_for_spec(spec)
            p.ensure_backup_data_pvc()
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
    The backup-data PVC is ownerless on purpose, so backups survive.

    What we DO here:
      1. Take a final backup Job (orphan — outlives the CR)
      2. Remove our finalizer so K8s can finish deletion
      3. Decrement running instance metrics

    Deletion is never blocked on backup success: a failed final backup is
    logged, the finalizer is still removed, and the pre-existing scheduled
    backups remain in the surviving backup-data PVC.
    """
    name      = meta["name"]
    namespace = meta["namespace"]
    uid       = meta.get("uid", "")
    api_ver   = f"{API_GROUP}/{API_VERSION}"

    logger.info(f"DELETE: {namespace}/{name}")

    if spec.get("backupEnabled", False):
        try:
            p = Provisioner(namespace, name, uid, spec, api_ver)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            job = p.build_final_backup_job(
                backup_image_for_spec(spec), f"{name}-final-backup-{ts}"
            )
            k8s_client.BatchV1Api().create_namespaced_job(namespace, job)
            logger.info(f"[{name}] Final backup Job created: {job.metadata.name}")
        except ApiException as e:
            logger.warning(f"[{name}] Final backup failed (non-blocking): {e.reason}")
        except Exception:
            logger.exception(f"[{name}] Final backup failed (non-blocking)")
    else:
        logger.info(f"[{name}] Backup disabled — no final backup taken")

    # Unblock deletion regardless of backup outcome.
    remove_finalizer(namespace, name)

    # Decrement the running instance gauge
    METRICS.instances_total.labels(namespace=namespace, phase=PHASE_RUNNING).dec()
    METRICS.instances_deleted.labels(namespace=namespace).inc()

    logger.info(
        f"[{name}] DELETE handler done. "
        "K8s will garbage-collect owned resources via owner references. "
        "Backup-data PVC is ownerless and retained."
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

        # Re-assert finalizer (self-heals objects created before finalizer support).
        ensure_finalizer(namespace, name, meta.get("finalizers"))

        # --- UID-based data-loss detection -----------------------------------
        stored_uid = (meta.get("annotations") or {}).get(LAST_PVC_UID_ANN)
        if stored_uid:
            try:
                current_pvc = p.core_v1.read_namespaced_persistent_volume_claim(
                    f"{name}-data", namespace
                )
                current_uid = current_pvc.metadata.uid
                if stored_uid != current_uid:
                    # PVC was recreated → data loss
                    logger.warning(f"[{name}] PVC UID changed! Stored: {stored_uid}, Current: {current_uid}")
                    emit_event(namespace, name,
                               event_type="Warning", reason="DataLoss",
                               message="Data PVC recreated; data loss detected.")
                    # Update stored UID to the new one
                    try:
                        api = _custom_api()
                        api.patch_namespaced_custom_object(
                            group=API_GROUP, version=API_VERSION,
                            namespace=namespace, plural="managedpostgres", name=name,
                            body={"metadata": {"annotations": {LAST_PVC_UID_ANN: current_uid}}}
                        )
                    except Exception as e:
                        logger.warning(f"[{name}] Failed to update PVC UID annotation: {e}")
                    # Trigger auto-restore from backup
                    maybe_auto_restore(namespace, name, spec, reason="PVC-UID-change")
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"[{name}] Could not read PVC for UID check: {e.reason}")

        results = {
            "secret":      p.ensure_secret()[0],
            "pvc":         p.ensure_pvc()[0],
            "statefulset": p.ensure_statefulset()[0],
            "service":     p.ensure_service()[0],
        }

        if spec.get("backupEnabled", False):
            p.ensure_backup_data_pvc()
            results["cronjob"] = p.ensure_backup_cronjob(
                backup_image_for_spec(spec)
            )[0]

        # Only log if something was recreated (drift detected and healed).
        # INFO, not WARNING: self-healing is the normal steady state.
        drifted = {k: v for k, v in results.items() if v != "unchanged"}
        if drifted:
            logger.info(f"[{name}] Drift detected and healed: {drifted}")
            sm = StatusManager(namespace, name, patch, status)
            sm.mark_running(p.endpoint())
            sm.set_observed_generation(meta.get("generation", 1))
            if results.get("pvc") == "created" and spec.get("backupEnabled", False):
                # Data PVC was gone and got recreated empty → data loss.
                # Backups live in the surviving backup-data PVC → auto-restore.
                maybe_auto_restore(namespace, name, spec, reason="data-pvc-recreated")

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

    Restore flow:
      1. Validate targetDatabase exists and backupFile is specified
      2. Create a K8s Job to perform the restore
      3. Job: initContainer downloads backup → main container runs psql
      4. Update PostgresRestore status: Pending → Running → Completed/Failed
    """
    name            = meta["name"]
    namespace       = meta["namespace"]
    target_database = spec.get("targetDatabase")
    backup_file     = spec.get("backupFile")

    logger.info(f"RESTORE: {namespace}/{name} → {target_database} from {backup_file}")

    if not target_database or not backup_file:
        raise kopf.PermanentError("targetDatabase and backupFile are required")

    # Fail fast with a clear message instead of a Job that can never succeed.
    target = get_managedpostgres(namespace, target_database)
    if target is None:
        raise kopf.PermanentError(
            f"targetDatabase '{target_database}' does not exist in namespace "
            f"'{namespace}'. Create the ManagedPostgres first."
        )
    db_spec = (target.get("spec") or {})

    backup_image = backup_image_for_spec(db_spec)

    sm = StatusManager(namespace, name, patch, status)
    sm.mark_creating()
    patch.status["phase"] = "Pending"
    patch.status["message"] = f"Creating restore Job for {target_database}"
    patch.status["startTime"] = datetime.now(timezone.utc).isoformat()

    try:
        mgr = RestoreManager(
            namespace, name, target_database, backup_file,
            spec, f"{API_GROUP}/{API_VERSION}", db_spec=db_spec,
        )
        action, job = mgr.create_job(backup_image)

        if action == "created":
            patch.status["phase"] = "Running"
            patch.status["message"] = f"Restore Job {job.metadata.name} created"
            patch.status["jobName"] = job.metadata.name
            METRICS.restores_started.labels(namespace=namespace).inc()
            logger.info(f"[{name}] Restore Job created: {job.metadata.name}")
            emit_event(namespace, target_database,
                       event_type="Normal", reason="RestoreStarted",
                       message=f"Restore of {target_database} from {backup_file} started.")
        else:
            patch.status["phase"] = "Running"
            patch.status["message"] = f"Restore Job already exists"
            patch.status["jobName"] = job.metadata.name

    except Exception as e:
        patch.status["phase"] = "Failed"
        patch.status["message"] = f"Failed to create restore Job: {e}"
        METRICS.restores_failed.labels(namespace=namespace).inc()
        logger.exception(f"[{name}] Failed to create restore Job")
        emit_event(namespace, target_database,
                   event_type="Warning", reason="RestoreFailed",
                   message=f"Failed to create restore Job: {e}")
        raise kopf.TemporaryError(str(e), delay=30)


# ── RESTORE RECONCILE TIMER ─────────────────────────────────────────────────────

@kopf.timer(API_GROUP, API_VERSION, "postgresrestores", interval=30.0, sharp=True)
def restore_reconcile(spec, meta, status, patch, **kwargs):
    """
    Reconcile PostgresRestore — check Job status and update CR status.

    Runs every 30 seconds until restore completes or fails.
    """
    name        = meta["name"]
    namespace   = meta["namespace"]
    target_db   = spec.get("targetDatabase")
    backup_file = spec.get("backupFile")
    job_name    = status.get("jobName", f"{name}-restore")

    current_phase = status.get("phase", "Pending")
    if current_phase in ("Completed", "Failed"):
        return  # Terminal state — nothing to do

    try:
        mgr = RestoreManager(namespace, name, target_db, backup_file, spec, f"{API_GROUP}/{API_VERSION}")
        job_status = mgr.get_job_status(job_name)

        new_phase = job_status["phase"]
        message   = job_status["message"]

        if new_phase != current_phase:
            patch.status["phase"] = new_phase
            patch.status["message"] = message
            if new_phase == "Completed":
                patch.status["completionTime"] = datetime.now(timezone.utc).isoformat()
                METRICS.restores_completed.labels(namespace=namespace).inc()
                logger.info(f"[{name}] Restore completed successfully")
                # Emit RestoreVerified event
                emit_event(namespace, target_db,
                           event_type="Normal", reason="RestoreVerified",
                           message="Restore completed successfully; backup receipt verified.")
                # Emit RecoveryCompleted event with duration
                start_time_str = status.get("startTime")
                if start_time_str:
                    try:
                        start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
                        recovery_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()
                        METRICS.restores_duration_seconds.labels(namespace=namespace).observe(recovery_seconds)
                        emit_event(namespace, target_db,
                                   event_type="Normal", reason="RecoveryCompleted",
                                   message=f"Recovery completed in {recovery_seconds:.0f}s")
                        # Update ManagedPostgres status message with recovery time
                        # (best effort - just log it)
                        logger.info(f"[{target_db}] Recovery took {recovery_seconds:.0f}s")
                    except Exception:
                        pass
            elif new_phase == "Failed":
                patch.status["completionTime"] = datetime.now(timezone.utc).isoformat()
                METRICS.restores_failed.labels(namespace=namespace).inc()
                logger.error(f"[{name}] Restore failed: {message}")
                emit_event(namespace, target_db,
                           event_type="Warning", reason="RestoreFailed",
                           message=f"Restore failed: {message}")

    except Exception as e:
        logger.error(f"[{name}] Restore reconcile error: {e}")
        # Don't raise — timer tick failure shouldn't crash operator


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
