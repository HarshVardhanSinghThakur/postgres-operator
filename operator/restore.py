"""
restore.py — Full restore Job implementation for PostgresRestore CR.

RESTORE FLOW:
  1. User creates PostgresRestore CR with targetDatabase + backupFile
  2. Operator creates a K8s Job to perform the restore
  3. Job has two containers (initContainer + main):
     - initContainer: downloads backup from S3 or local PVC to shared volume
     - main container: runs psql < backup.sql.gz to restore
  4. Job updates PostgresRestore status: Pending → Running → Completed/Failed
  5. On completion, operator marks target ManagedPostgres as restored

WHY JOB NOT CRONJOB:
  CronJob = recurring schedule. Job = one-time task. Restore is one-time.

WHY INITCONTAINER:
  - Downloads backup before main container starts
  - Shares volume with main container (emptyDir or PVC)
  - Keeps main container simple: just psql
  - If download fails, Job fails fast without touching DB

VOLUME STRATEGY:
  - For local backend: mount the same PVC that CronJob writes to
  - For S3: use emptyDir, initContainer downloads from S3 to it
"""

import logging
from typing import Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from config import (
    API_VERSION,
    LOCAL_BACKUP_PATH,
    COMPONENT_BACKUP,
    resource_labels,
    backup_data_pvc_name,
)
from metrics import METRICS

logger = logging.getLogger(__name__)


class RestoreManager:
    """
    Manages the restore Job lifecycle for a PostgresRestore resource.
    """

    def __init__(
        self,
        namespace: str,
        restore_name: str,
        target_database: str,
        backup_file: str,
        spec: dict,
        api_version: str,
        db_spec: Optional[dict] = None,
    ):
        self.namespace = namespace
        self.restore_name = restore_name
        self.target_database = target_database
        self.backup_file = backup_file
        self.spec = spec
        self.api_version = api_version
        # Spec of the target ManagedPostgres (backend + s3Config). Optional so
        # existing callers that only pass the restore spec keep working.
        self.db_spec = db_spec or {}

        self.batch_v1 = client.BatchV1Api()
        self.core_v1 = client.CoreV1Api()

        self.labels = resource_labels(target_database, COMPONENT_BACKUP)
        self.labels["postgres-restore"] = restore_name

    def build_restore_job(self, backup_image: str) -> client.V1Job:
        """
        Build the restore Job spec.

        The Job:
        1. initContainer: downloads backup to /backup/<backup_file>
        2. main container: gunzip -c /backup/<backup_file> | psql -h <host> -U postgres -d <db>
        """
        # NOTE: self.spec here is the PostgresRestore spec (targetDatabase +
        # backupFile). Backend settings live on the target ManagedPostgres,
        # which the caller passes in as db_spec. Fall back to local defaults
        # when the caller doesn't provide it (e.g. unit tests).
        db_spec = getattr(self, "db_spec", {}) or {}
        backend = db_spec.get("backupBackend", "local")
        s3_config = db_spec.get("s3Config", {}) if backend == "s3" else {}

        # Shared scratch volume for the staged backup file
        volumes = [
            client.V1Volume(
                name="backup-data",
                empty_dir=client.V1EmptyDirVolumeSource(),
            )
        ]

        # InitContainer: stage backup into /backup/
        if backend == "s3":
            init_container = self._build_s3_download_container(s3_config)
        else:
            init_container = self._build_local_copy_container()
            # Mount the surviving backup-data PVC so initContainer can read it.
            # The PVC is ownerless (see Provisioner.ensure_backup_data_pvc),
            # so it outlives the ManagedPostgres that created it.
            volumes.append(
                client.V1Volume(
                    name="backup-store",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=backup_data_pvc_name(self.target_database)
                    ),
                )
            )

        # Main container: restore via psql
        main_container = self._build_restore_container()

        job_name = f"{self.restore_name}-restore"

        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=self.namespace,
                labels=self.labels,
                annotations={
                    "postgres-operator.restore/target": self.target_database,
                    "postgres-operator.restore/backup-file": self.backup_file,
                },
                # NOTE: no owner_references — the Job must survive even if the
                # PostgresRestore CR is deleted while it runs.
            ),
            spec=client.V1JobSpec(
                backoff_limit=0,  # Don't retry — restore is idempotent-ish but we want manual retry
                ttl_seconds_after_finished=3600,  # Clean up after 1 hour
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=self.labels),
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        init_containers=[init_container],
                        containers=[main_container],
                        volumes=volumes,
                    ),
                ),
            ),
        )
        return job

    def _build_s3_download_container(self, s3_config: dict) -> client.V1Container:
        """InitContainer that downloads backup from S3 into /backup/."""
        bucket = s3_config.get("bucket")
        region = s3_config.get("region", "us-east-1")
        creds_secret = s3_config.get("credentialsSecret")

        env = [
            client.V1EnvVar(name="S3_BUCKET", value=bucket),
            client.V1EnvVar(name="S3_REGION", value=region),
            client.V1EnvVar(name="BACKUP_FILE", value=self.backup_file),
            client.V1EnvVar(name="TARGET_DATABASE", value=self.target_database),
            client.V1EnvVar(name="NAMESPACE", value=self.namespace),
        ]

        # AWS credentials from secret
        if creds_secret:
            env.extend([
                client.V1EnvVar(
                    name="AWS_ACCESS_KEY_ID",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=creds_secret, key="AWS_ACCESS_KEY_ID"
                        )
                    ),
                ),
                client.V1EnvVar(
                    name="AWS_SECRET_ACCESS_KEY",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=creds_secret, key="AWS_SECRET_ACCESS_KEY"
                        )
                    ),
                ),
            ])

        return client.V1Container(
            name="download-backup",
            image="amazon/aws-cli:latest",
            command=["sh", "-c"],
            args=[
                "set -e && mkdir -p /backup && "
                # backupFile "latest" resolves to the newest object at runtime,
                # so auto-restore never needs to know the exact filename.
                'if [ "${BACKUP_FILE}" = "latest" ]; then '
                'KEY=$(aws s3 ls "s3://${S3_BUCKET}/${NAMESPACE}/${TARGET_DATABASE}/" --recursive '
                '| sort -k1,2 | tail -n 1 | awk \'{print $4}\'); '
                'if [ -z "$KEY" ]; then echo "No backups found in S3 prefix" >&2; exit 1; fi; '
                'aws s3 cp "s3://${S3_BUCKET}/${KEY}" "/backup/$(basename $KEY)" && '
                'echo "Downloaded latest: $KEY"; '
                "else "
                'aws s3 cp "s3://${S3_BUCKET}/${NAMESPACE}/${TARGET_DATABASE}/${BACKUP_FILE}" '
                '"/backup/${BACKUP_FILE}" && '
                "echo 'Download complete'; "
                "fi"
            ],
            env=env,
            volume_mounts=[
                client.V1VolumeMount(name="backup-data", mount_path="/backup")
            ],
        )

    def _build_local_copy_container(self) -> client.V1Container:
        """
        InitContainer that stages a backup from the backup-data PVC into /backup/.

        The backup-store volume is the dedicated <instance>-backup-data PVC
        created by Provisioner.ensure_backup_data_pvc (same PVC the CronJob
        writes to). backupFile "latest" picks the newest dump at runtime.
        """
        return client.V1Container(
            name="copy-backup",
            image="busybox:latest",
            command=["sh", "-c"],
            args=[
                "set -e && mkdir -p /backup && "
                f'SRC_DIR="{LOCAL_BACKUP_PATH}/{self.target_database}" && '
                f'WANT="{self.backup_file}" && '
                'if [ "$WANT" = "latest" ]; then '
                'SRC=$(ls -t "$SRC_DIR"/*.sql.gz 2>/dev/null | head -n 1); '
                'if [ -z "$SRC" ]; then echo "No backups found in $SRC_DIR" >&2; exit 1; fi; '
                "else SRC=\"$SRC_DIR/$WANT\"; "
                'if [ ! -f "$SRC" ]; then echo "Backup not found: $SRC" >&2; exit 1; fi; '
                "fi && "
                'cp "$SRC" /backup/ && '
                'echo "Staged: $SRC"'
            ],
            volume_mounts=[
                client.V1VolumeMount(name="backup-data", mount_path="/backup"),
                client.V1VolumeMount(
                    name="backup-store", mount_path=LOCAL_BACKUP_PATH, read_only=True
                ),
            ],
        )

    def _build_restore_container(self) -> client.V1Container:
        """Main container that runs psql restore from the staged file."""
        # Get credentials from the target database's Secret
        password_env = client.V1EnvVar(
            name="PGPASSWORD",
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(
                    name=f"{self.target_database}-credentials",
                    key="POSTGRES_PASSWORD",
                )
            ),
        )

        host = f"{self.target_database}.{self.namespace}.svc.cluster.local"

        return client.V1Container(
            name="restore",
            image="postgres:15-alpine",
            command=["sh", "-c"],
            args=[
                "set -e && "
                "echo 'Waiting for Postgres to be ready...' && "
                f"until pg_isready -h {host} -U postgres; do sleep 2; done && "
                "echo 'Starting restore...' && "
                # With backupFile "latest" the initContainer staged exactly one
                # file; otherwise use the requested filename.
                f'FILE="/backup/{self.backup_file}" && '
                'if [ ! -f "$FILE" ]; then '
                'FILE=$(ls -t /backup/*.sql.gz 2>/dev/null | head -n 1); '
                'if [ -z "$FILE" ]; then echo "No staged backup in /backup" >&2; exit 1; fi; '
                "fi && "
                f"echo \"Restoring $FILE\" && "
                f"gunzip -c \"$FILE\" | "
                f"psql -h {host} -U postgres -d {self.target_database} -v ON_ERROR_STOP=1 && "
                "echo 'Restore completed successfully'"
            ],
            env=[
                password_env,
                client.V1EnvVar(name="BACKUP_FILE", value=self.backup_file),
            ],
            volume_mounts=[
                client.V1VolumeMount(name="backup-data", mount_path="/backup", read_only=True),
            ],
        )

    def create_job(self, backup_image: str) -> tuple[str, Optional[client.V1Job]]:
        """Create the restore Job. Returns (action, job)."""
        job_name = f"{self.restore_name}-restore"
        existing = self._get_job(job_name)

        if existing:
            logger.info(f"Restore Job {job_name} already exists")
            return "exists", existing

        job = self.build_restore_job(backup_image)
        result = self.batch_v1.create_namespaced_job(self.namespace, job)
        logger.info(f"Created restore Job: {job_name}")
        METRICS.restores_started.labels(namespace=self.namespace).inc()
        return "created", result

    def _get_job(self, name: str) -> Optional[client.V1Job]:
        try:
            return self.batch_v1.read_namespaced_job(name, self.namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def get_job_status(self, job_name: str) -> dict:
        """Get Job status and determine restore phase."""
        try:
            job = self.batch_v1.read_namespaced_job(job_name, self.namespace)
            status = job.status

            if status.succeeded and status.succeeded > 0:
                return {"phase": "Completed", "message": "Restore completed successfully"}
            elif status.failed and status.failed > 0:
                return {"phase": "Failed", "message": "Restore Job failed"}
            elif status.active and status.active > 0:
                return {"phase": "Running", "message": "Restore in progress"}
            else:
                return {"phase": "Pending", "message": "Job not yet started"}
        except ApiException as e:
            if e.status == 404:
                return {"phase": "Failed", "message": "Restore Job not found"}
            raise


def build_restore_job_manifest(
    namespace: str,
    restore_name: str,
    target_database: str,
    backup_file: str,
    spec: dict,
    backup_image: str,
) -> dict:
    """
    Standalone function to generate Job manifest (for debugging/kubectl apply).
    """
    mgr = RestoreManager(namespace, restore_name, target_database, backup_file, spec, API_VERSION)
    job = mgr.build_restore_job(backup_image)
    return client.ApiClient().sanitize_for_serialization(job)