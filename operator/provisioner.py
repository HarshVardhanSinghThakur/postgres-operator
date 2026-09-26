"""
provisioner.py — Creates, patches, and verifies all Kubernetes resources.

THE IDEMPOTENCY PROBLEM:
  Imagine the operator creates a Secret successfully, then crashes
  before creating the StatefulSet. When it restarts, kopf will
  fire the CREATE handler again for the same resource.

  Naive code:
    kubernetes.create_secret(...)   ← FAILS: "Secret already exists"
    kubernetes.create_statefulset() ← never reached

  Idempotent code (what we do):
    try to GET the resource first
    if it exists → check if it needs updating → patch if yes
    if it doesn't exist → create it

  This means our handlers can safely run multiple times with the
  same input and always produce the same result. This is the
  reconciler pattern at its core.

HASH-BASED CHANGE DETECTION:
  When the user updates their ManagedPostgres spec, we need to
  figure out which K8s resources need to be updated.

  We store a hash of the spec used to create each resource
  as an annotation on that resource. On reconcile:
    current_hash = hash(current_spec)
    stored_hash  = resource.annotations["last-applied-hash"]
    if current_hash != stored_hash → spec changed → patch
    else → nothing to do (noop)

  This avoids unnecessary API calls and prevents drift.

OWNER REFERENCES:
  Every resource we create has an ownerReference pointing to
  the ManagedPostgres object. When ManagedPostgres is deleted,
  K8s automatically garbage-collects all owned resources.
  We don't need a delete handler for individual resources.
"""

import hashlib
import json
import logging
import secrets
import string
from typing import Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from config import (
    API_GROUP, API_VERSION,
    ANNOTATION_LAST_APPLIED, ANNOTATION_VERSION, OPERATOR_VERSION,
    LABEL_MANAGED_BY, COMPONENT_DB, COMPONENT_BACKUP,
    DEFAULT_CPU_REQUEST, DEFAULT_MEMORY_REQUEST,
    DEFAULT_CPU_LIMIT, DEFAULT_MEMORY_LIMIT,
    DEFAULT_BACKUP_STORAGE, LOCAL_BACKUP_PATH,
    resource_labels, postgres_image,
    backup_data_pvc_name,
)
from metrics import METRICS

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def spec_hash(spec: dict) -> str:
    """
    Deterministic SHA-256 hash of a spec dict.
    Used to detect whether a resource needs to be updated.

    sorted_keys=True ensures the hash is the same regardless
    of key insertion order (Python dicts preserve order but
    we don't want to depend on that).
    """
    canonical = json.dumps(spec, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]  # 16 chars is enough


def generate_password(length: int = 24) -> str:
    """
    Cryptographically secure random password.

    Uses secrets module (not random) — secrets is designed
    for generating tokens, passwords, and secrets.
    Alphabet excludes chars that confuse psql connection strings.
    """
    alphabet = string.ascii_letters + string.digits + "!@#%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def make_owner_reference(name: str, uid: str, api_version: str) -> dict:
    """
    Owner reference that links a child resource to its ManagedPostgres parent.

    blockOwnerDeletion=True: K8s won't delete the parent until
    all children are gone. Prevents orphaned resources.

    controller=True: tells K8s this operator is the controller
    for these resources (not just an owner).
    """
    return {
        "apiVersion":         api_version,
        "kind":               "ManagedPostgres",
        "name":               name,
        "uid":                uid,
        "controller":         True,
        "blockOwnerDeletion": True,
    }


def resource_exists(get_fn, *args, **kwargs) -> Optional[object]:
    """
    Call a K8s GET function. Return the object if found, None if 404.
    Re-raise any other API errors.

    This is the foundation of idempotency — check before create.
    """
    try:
        return get_fn(*args, **kwargs)
    except ApiException as e:
        if e.status == 404:
            return None
        raise   # 403 Forbidden, 500 Server Error, etc. — propagate up


def needs_update(resource, current_hash: str) -> bool:
    """
    Check if a resource's stored hash differs from the current spec hash.
    If they differ, the spec changed and we need to patch.
    """
    annotations = resource.metadata.annotations or {}
    stored_hash = annotations.get(ANNOTATION_LAST_APPLIED, "")
    return stored_hash != current_hash


def record_api_call(operation: str, resource_type: str, success: bool):
    """Increment the K8s API call counter metric."""
    METRICS.k8s_api_calls.labels(
        operation=operation,
        resource=resource_type,
        result="success" if success else "failure",
    ).inc()


# ── Provisioner class ─────────────────────────────────────────────────────────

class Provisioner:
    """
    Creates and reconciles all K8s resources for one ManagedPostgres instance.

    Each public method is idempotent:
      - Calls resource_exists() before creating
      - Checks needs_update() before patching
      - Returns a (action, resource) tuple so the caller knows what happened
        action: "created" | "updated" | "unchanged"

    USAGE (from main.py handler):
      p = Provisioner(namespace, name, uid, spec, api_version)
      p.ensure_secret()
      p.ensure_pvc()
      p.ensure_statefulset()
      p.ensure_service()
      if spec.get("backupEnabled"):
          p.ensure_backup_cronjob()
    """

    def __init__(
        self,
        namespace:   str,
        name:        str,
        uid:         str,
        spec:        dict,
        api_version: str,
    ):
        self.namespace   = namespace
        self.name        = name
        self.uid         = uid
        self.spec        = spec
        self.api_version = api_version

        # K8s API clients
        self.core_v1  = client.CoreV1Api()      # Secrets, PVCs, Services, Pods
        self.apps_v1  = client.AppsV1Api()       # StatefulSets, Deployments
        self.batch_v1 = client.BatchV1Api()      # CronJobs, Jobs

        # Owner reference attached to every resource we create
        self.owner_ref = make_owner_reference(name, uid, api_version)

        # Hash of current spec — used for change detection
        self.current_hash = spec_hash(spec)

        # Standard labels for all resources
        self.labels = resource_labels(name, COMPONENT_DB)

        # Standard annotations for all resources
        self.annotations = {
            ANNOTATION_LAST_APPLIED: self.current_hash,
            ANNOTATION_VERSION:      OPERATOR_VERSION,
        }

    # ── Secret ────────────────────────────────────────────────────────────────

    def ensure_secret(self) -> tuple[str, object]:
        """
        Ensure a Secret exists containing Postgres credentials.

        IMPORTANT: We only generate a new password if the Secret
        doesn't exist. If it exists, we never regenerate the password
        — that would break existing connections.

        Secret name: <instance-name>-credentials
        """
        secret_name = f"{self.name}-credentials"
        existing    = resource_exists(
            self.core_v1.read_namespaced_secret,
            secret_name, self.namespace
        )

        if existing:
            # Secret exists — check if labels/annotations need updating
            # We NEVER touch the data (password) field on update
            if not needs_update(existing, self.current_hash):
                logger.debug(f"Secret {secret_name}: unchanged")
                return "unchanged", existing

            # Patch metadata only, leave data alone
            patch = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    annotations=self.annotations,
                    labels=self.labels,
                )
            )
            result = self.core_v1.patch_namespaced_secret(
                secret_name, self.namespace, patch
            )
            record_api_call("patch", "secret", True)
            logger.info(f"Secret {secret_name}: patched metadata")
            return "updated", result

        # Secret does not exist — create with fresh password
        password = generate_password()
        secret   = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=self.namespace,
                labels=self.labels,
                annotations=self.annotations,
                owner_references=[self.owner_ref],
            ),
            # Immutable: data can never be changed after creation, only
            # metadata. Protects credentials from accidental modification.
            # (Metadata patches in the update path above are still allowed.)
            immutable=True,
            # string_data: K8s base64-encodes these for us
            string_data={
                "POSTGRES_USER":     "postgres",
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB":       self.name,
                # Connection string — apps can use this directly
                "DATABASE_URL": (
                    f"postgresql://postgres:{password}"
                    f"@{self.name}.{self.namespace}.svc.cluster.local"
                    f":5432/{self.name}"
                ),
            },
        )

        result = self.core_v1.create_namespaced_secret(self.namespace, secret)
        record_api_call("create", "secret", True)
        logger.info(f"Secret {secret_name}: created")
        return "created", result

    # ── PersistentVolumeClaim ─────────────────────────────────────────────────

    def ensure_pvc(self) -> tuple[str, object]:
        """
        Ensure a PVC exists for Postgres data storage.

        PVC SIZE IS IMMUTABLE (mostly):
          Once a PVC is created, you can only increase its size
          if the StorageClass has allowVolumeExpansion=true.
          We detect size changes and attempt a patch, but log
          a warning if the StorageClass doesn't support it.

        PVC name: <instance-name>-data
        """
        pvc_name = f"{self.name}-data"
        storage  = self.spec.get("storage", "5Gi")
        existing = resource_exists(
            self.core_v1.read_namespaced_persistent_volume_claim,
            pvc_name, self.namespace
        )

        if existing:
            if not needs_update(existing, self.current_hash):
                logger.debug(f"PVC {pvc_name}: unchanged")
                return "unchanged", existing

            # Only storage size could meaningfully change
            # Attempt patch — may fail if StorageClass doesn't support expansion
            try:
                patch = client.V1PersistentVolumeClaim(
                    metadata=client.V1ObjectMeta(annotations=self.annotations),
                    spec=client.V1PersistentVolumeClaimSpec(
                        resources=client.V1ResourceRequirements(
                            requests={"storage": storage}
                        )
                    )
                )
                result = self.core_v1.patch_namespaced_persistent_volume_claim(
                    pvc_name, self.namespace, patch
                )
                record_api_call("patch", "pvc", True)
                logger.info(f"PVC {pvc_name}: storage patched to {storage}")
                return "updated", result
            except ApiException as e:
                # 422 Unprocessable Entity = storage class doesn't allow expansion
                logger.warning(
                    f"PVC {pvc_name}: storage resize to {storage} rejected "
                    f"(StorageClass may not support expansion): {e.reason}"
                )
                return "unchanged", existing

        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=pvc_name,
                namespace=self.namespace,
                labels=self.labels,
                annotations=self.annotations,
                owner_references=[self.owner_ref],
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],  # one pod at a time (fine for primary)
                resources=client.V1ResourceRequirements(
                    requests={"storage": storage}
                ),
                # No storageClassName = use cluster default
                # In real prod you'd parameterize this
            ),
        )

        result = self.core_v1.create_namespaced_persistent_volume_claim(
            self.namespace, pvc
        )
        record_api_call("create", "pvc", True)
        logger.info(f"PVC {pvc_name}: created ({storage})")
        return "created", result

    # ── StatefulSet ───────────────────────────────────────────────────────────

    def ensure_statefulset(self) -> tuple[str, object]:
        """
        Ensure a StatefulSet running Postgres exists.

        WHY STATEFULSET NOT DEPLOYMENT:
          Deployments are for stateless pods — any pod is interchangeable.
          StatefulSets give pods:
            - Stable, predictable names (postgres-0, postgres-1)
            - Ordered startup and shutdown
            - Stable network identity
          These matter for Postgres because replication and failover
          depend on knowing which pod is which.

        StatefulSet name: <instance-name>
        """
        version  = self.spec.get("version", "15")
        storage  = self.spec.get("storage", "5Gi")
        pvc_name = f"{self.name}-data"
        image    = postgres_image(version)

        resources_spec = self.spec.get("resources", {})
        requests = resources_spec.get("requests", {})
        limits   = resources_spec.get("limits", {})

        # The pod template spec — what each pod looks like
        pod_spec = client.V1PodSpec(
            containers=[
                client.V1Container(
                    name="postgres",
                    image=image,
                    ports=[client.V1ContainerPort(container_port=5432, name="postgres")],

                    # Pull credentials from the Secret we created
                    env_from=[
                        client.V1EnvFromSource(
                            secret_ref=client.V1SecretEnvSource(
                                name=f"{self.name}-credentials"
                            )
                        )
                    ],

                    # Mount the PVC at Postgres's data directory
                    volume_mounts=[
                        client.V1VolumeMount(
                            name="data",
                            mount_path="/var/lib/postgresql/data",
                            sub_path="pgdata",   # avoids lost+found issue on some StorageClasses
                        )
                    ],

                    # Resource requests and limits
                    resources=client.V1ResourceRequirements(
                        requests={
                            "cpu":    requests.get("cpu",    DEFAULT_CPU_REQUEST),
                            "memory": requests.get("memory", DEFAULT_MEMORY_REQUEST),
                        },
                        limits={
                            "cpu":    limits.get("cpu",    DEFAULT_CPU_LIMIT),
                            "memory": limits.get("memory", DEFAULT_MEMORY_LIMIT),
                        },
                    ),

                    # ── Health probes ──────────────────────────────────────────
                    # liveness:  if this fails repeatedly, K8s restarts the pod
                    # readiness: if this fails, K8s removes pod from Service endpoints
                    #            (stops sending traffic to it)

                    liveness_probe=client.V1Probe(
                        exec=client.V1ExecAction(
                            command=["pg_isready", "-U", "postgres"]
                        ),
                        initial_delay_seconds=30,   # give postgres time to start
                        period_seconds=10,
                        failure_threshold=6,         # 6 failures = 60s before restart
                    ),

                    readiness_probe=client.V1Probe(
                        exec=client.V1ExecAction(
                            command=["pg_isready", "-U", "postgres"]
                        ),
                        initial_delay_seconds=5,
                        period_seconds=5,
                        failure_threshold=3,
                    ),
                )
            ],

            # Reference the PVC — but we use an existing PVC, not a volumeClaimTemplate
            # (volumeClaimTemplates create a new PVC per pod, we want one shared PVC)
            volumes=[
                client.V1Volume(
                    name="data",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=pvc_name
                    )
                )
            ],
        )

        statefulset_body = client.V1StatefulSet(
            metadata=client.V1ObjectMeta(
                name=self.name,
                namespace=self.namespace,
                labels=self.labels,
                annotations=self.annotations,
                owner_references=[self.owner_ref],
            ),
            spec=client.V1StatefulSetSpec(
                replicas=1,   # primary only for now (replicas feature = Phase 2)
                selector=client.V1LabelSelector(
                    match_labels={LABEL_MANAGED_BY: "postgres-operator",
                                  "app.kubernetes.io/instance": self.name}
                ),
                service_name=self.name,   # headless service name (required by StatefulSet)
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=self.labels),
                    spec=pod_spec,
                ),
                # Rolling update: update one pod at a time, check readiness before next
                update_strategy=client.V1StatefulSetUpdateStrategy(
                    type="RollingUpdate"
                ),
            ),
        )

        existing = resource_exists(
            self.apps_v1.read_namespaced_stateful_set,
            self.name, self.namespace
        )

        if existing:
            if not needs_update(existing, self.current_hash):
                logger.debug(f"StatefulSet {self.name}: unchanged")
                return "unchanged", existing

            result = self.apps_v1.patch_namespaced_stateful_set(
                self.name, self.namespace, statefulset_body
            )
            record_api_call("patch", "statefulset", True)
            logger.info(f"StatefulSet {self.name}: patched (image={image})")
            return "updated", result

        result = self.apps_v1.create_namespaced_stateful_set(
            self.namespace, statefulset_body
        )
        record_api_call("create", "statefulset", True)
        logger.info(f"StatefulSet {self.name}: created (image={image})")
        return "created", result

    # ── Service ───────────────────────────────────────────────────────────────

    def ensure_service(self) -> tuple[str, object]:
        """
        Ensure a ClusterIP Service exists for stable DNS-based access.

        WHY WE NEED THIS:
          Pods have ephemeral IPs. Every time a pod restarts, its IP changes.
          A Service gives a stable virtual IP and DNS name.
          Other pods connect to: <name>.<namespace>.svc.cluster.local:5432
          They don't care which pod answers — the Service routes to healthy pods.

        Service name: <instance-name>
        DNS name:     <instance-name>.<namespace>.svc.cluster.local
        """
        selector_labels = {
            "app.kubernetes.io/managed-by": "postgres-operator",
            "app.kubernetes.io/instance":   self.name,
        }

        service_body = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=self.name,
                namespace=self.namespace,
                labels=self.labels,
                annotations=self.annotations,
                owner_references=[self.owner_ref],
            ),
            spec=client.V1ServiceSpec(
                type="ClusterIP",       # only accessible within the cluster
                selector=selector_labels,
                ports=[
                    client.V1ServicePort(
                        name="postgres",
                        port=5432,
                        target_port=5432,
                        protocol="TCP",
                    )
                ],
            ),
        )

        existing = resource_exists(
            self.core_v1.read_namespaced_service,
            self.name, self.namespace
        )

        if existing:
            if not needs_update(existing, self.current_hash):
                logger.debug(f"Service {self.name}: unchanged")
                return "unchanged", existing

            result = self.core_v1.patch_namespaced_service(
                self.name, self.namespace, service_body
            )
            record_api_call("patch", "service", True)
            logger.info(f"Service {self.name}: patched")
            return "updated", result

        result = self.core_v1.create_namespaced_service(
            self.namespace, service_body
        )
        record_api_call("create", "service", True)
        logger.info(f"Service {self.name}: created")
        return "created", result

    # ── Backup data PVC ─────────────────────────────────────────────────────

    def ensure_backup_data_pvc(self) -> tuple[str, object]:
        """
        Ensure the dedicated backup-data PVC exists (local backend only).

        CRITICAL: this PVC is created WITHOUT an owner reference on purpose.
        Backup CronJobs and restore Jobs mount it at /backups. When the
        ManagedPostgres is deleted, K8s garbage-collects owned resources —
        but this PVC survives, so a later instance with the same name can
        restore from the retained backups. This is what makes
        "delete database → restore everything" work.

        PVC name: <instance-name>-backup-data
        Returns ("created" | "exists", pvc). "exists" (not "unchanged") signals
        a pre-existing PVC — callers use this to decide on auto-restore.
        """
        pvc_name = backup_data_pvc_name(self.name)
        storage  = self.spec.get("backupStorage", DEFAULT_BACKUP_STORAGE)
        existing = resource_exists(
            self.core_v1.read_namespaced_persistent_volume_claim,
            pvc_name, self.namespace
        )

        if existing:
            logger.debug(f"Backup PVC {pvc_name}: exists")
            return "exists", existing

        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=pvc_name,
                namespace=self.namespace,
                labels=resource_labels(self.name, COMPONENT_BACKUP),
                # NOTE: no owner_references — must survive CR deletion.
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": storage}
                ),
            ),
        )

        result = self.core_v1.create_namespaced_persistent_volume_claim(
            self.namespace, pvc
        )
        record_api_call("create", "pvc", True)
        logger.info(f"Backup PVC {pvc_name}: created ({storage})")
        return "created", result

    # ── Backup container spec builders (shared by CronJob + final Job) ──────

    def _backup_env_vars(self) -> list:
        """Env vars passed to the backup container."""
        backend = self.spec.get("backupBackend", "local")
        retention = str(self.spec.get("backupRetentionDays", 7))

        env_vars = [
            client.V1EnvVar(name="DB_NAME",    value=self.name),
            client.V1EnvVar(name="NAMESPACE",  value=self.namespace),
            client.V1EnvVar(name="BACKEND",    value=backend),
            client.V1EnvVar(name="RETENTION_DAYS", value=retention),
            # Postgres credentials from the Secret
            client.V1EnvVar(
                name="POSTGRES_HOST",
                value=f"{self.name}.{self.namespace}.svc.cluster.local"
            ),
            client.V1EnvVar(
                name="POSTGRES_PASSWORD",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=f"{self.name}-credentials",
                        key="POSTGRES_PASSWORD"
                    )
                )
            ),
        ]

        # Add S3 config if backend is s3
        if backend == "s3":
            s3_config          = self.spec.get("s3Config", {})
            s3_creds_secret    = s3_config.get("credentialsSecret", "")
            env_vars += [
                client.V1EnvVar(name="S3_BUCKET", value=s3_config.get("bucket", "")),
                client.V1EnvVar(name="S3_REGION", value=s3_config.get("region", "us-east-1")),
                client.V1EnvVar(
                    name="AWS_ACCESS_KEY_ID",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=s3_creds_secret,
                            key="AWS_ACCESS_KEY_ID"
                        )
                    )
                ),
                client.V1EnvVar(
                    name="AWS_SECRET_ACCESS_KEY",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=s3_creds_secret,
                            key="AWS_SECRET_ACCESS_KEY"
                        )
                    )
                ),
            ]

        return env_vars

    def _backup_volumes(self) -> list:
        """
        Volumes for backup pods. Local backend mounts the dedicated backup-data
        PVC at /backups so backups survive pod restarts. S3 needs no volumes
        (boto3 streams straight to the bucket).
        """
        if self.spec.get("backupBackend", "local") == "s3":
            return []
        return [
            client.V1Volume(
                name="backup-store",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=backup_data_pvc_name(self.name)
                ),
            )
        ]

    def _backup_volume_mounts(self) -> list:
        """Volume mounts for the backup container (pairs with _backup_volumes)."""
        if self.spec.get("backupBackend", "local") == "s3":
            return []
        return [
            client.V1VolumeMount(
                name="backup-store",
                mount_path=LOCAL_BACKUP_PATH,   # /backups — matches backup.py LocalBackend
            )
        ]

    def _backup_pod_spec(self, backup_image: str, labels: dict) -> client.V1PodSpec:
        """PodSpec for one backup run. Shared by the CronJob and the final-backup Job."""
        return client.V1PodSpec(
            restart_policy="OnFailure",
            containers=[
                client.V1Container(
                    name="backup",
                    image=backup_image,
                    env=self._backup_env_vars(),
                    volume_mounts=self._backup_volume_mounts(),
                )
            ],
            volumes=self._backup_volumes(),
        )

    # ── Backup CronJob ────────────────────────────────────────────────────────

    def ensure_backup_cronjob(self, backup_image: str) -> tuple[str, object]:
        """
        Ensure a CronJob exists that runs backups on schedule.

        The CronJob spins up a backup container that:
          1. Runs pg_dump against our Postgres pod
          2. Compresses the output
          3. Saves to the backup-data PVC (local) or uploads to S3

        backup_image: resolved via config.backup_image_for_spec() by the caller.

        CronJob name: <instance-name>-backup
        """
        cj_name  = f"{self.name}-backup"
        schedule = self.spec.get("backupSchedule", "0 2 * * *")
        backend  = self.spec.get("backupBackend", "local")

        backup_labels = resource_labels(self.name, COMPONENT_BACKUP)

        cronjob_body = client.V1CronJob(
            metadata=client.V1ObjectMeta(
                name=cj_name,
                namespace=self.namespace,
                labels=backup_labels,
                annotations=self.annotations,
                owner_references=[self.owner_ref],
            ),
            spec=client.V1CronJobSpec(
                schedule=schedule,
                concurrency_policy="Forbid",      # don't run if previous is still running
                successful_jobs_history_limit=3,   # keep last 3 successful job pods
                failed_jobs_history_limit=3,        # keep last 3 failed job pods for debugging
                job_template=client.V1JobTemplateSpec(
                    spec=client.V1JobSpec(
                        backoff_limit=2,           # retry failed backup up to 2 times
                        template=client.V1PodTemplateSpec(
                            metadata=client.V1ObjectMeta(labels=backup_labels),
                            spec=self._backup_pod_spec(backup_image, backup_labels),
                        ),
                    ),
                ),
            ),
        )

        existing = resource_exists(
            self.batch_v1.read_namespaced_cron_job,
            cj_name, self.namespace
        )

        if existing:
            if not needs_update(existing, self.current_hash):
                logger.debug(f"CronJob {cj_name}: unchanged")
                return "unchanged", existing

            result = self.batch_v1.patch_namespaced_cron_job(
                cj_name, self.namespace, cronjob_body
            )
            record_api_call("patch", "cronjob", True)
            logger.info(f"CronJob {cj_name}: patched (schedule={schedule})")
            return "updated", result

        result = self.batch_v1.create_namespaced_cron_job(
            self.namespace, cronjob_body
        )
        record_api_call("create", "cronjob", True)
        logger.info(f"CronJob {cj_name}: created (schedule={schedule}, backend={backend})")
        return "created", result

    def build_final_backup_job(self, backup_image: str, job_name: str) -> client.V1Job:
        """
        Build a one-shot final backup Job (used by the delete handler).

        IMPORTANT: no owner reference — it must survive the ManagedPostgres
        deletion so the backup actually completes. The backup-data PVC is also
        ownerless, so the dump file is retained for a later restore.
        """
        backup_labels = resource_labels(self.name, COMPONENT_BACKUP)

        return client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=self.namespace,
                labels=backup_labels,
                annotations={
                    "db.harshdev.io/final-backup-for": self.name,
                },
                # NOTE: no owner_references — must outlive the CR being deleted.
            ),
            spec=client.V1JobSpec(
                backoff_limit=1,
                ttl_seconds_after_finished=86400,   # keep a day for debugging
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=backup_labels),
                    spec=self._backup_pod_spec(backup_image, backup_labels),
                ),
            ),
        )

    def endpoint(self) -> str:
        """DNS endpoint for this database instance."""
        return f"{self.name}.{self.namespace}.svc.cluster.local:5432"
