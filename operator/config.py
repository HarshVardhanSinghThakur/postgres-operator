"""
config.py — Operator-wide constants and configuration.

Single source of truth for all labels, annotations, and defaults.
If a value appears more than once in the codebase, it lives here.
"""

# ── API group ────────────────────────────────────────────────────────────────
API_GROUP    = "db.harshdev.io"
API_VERSION  = "v1"

# ── Label keys ───────────────────────────────────────────────────────────────
# Every resource the operator creates gets these labels.
# They let us list/filter "all resources owned by this operator"
# and "all resources owned by this specific ManagedPostgres instance".

LABEL_MANAGED_BY  = "app.kubernetes.io/managed-by"   # K8s convention
LABEL_COMPONENT   = "app.kubernetes.io/component"
LABEL_INSTANCE    = "app.kubernetes.io/instance"      # the ManagedPostgres name
LABEL_PART_OF     = "app.kubernetes.io/part-of"

OPERATOR_NAME     = "postgres-operator"
COMPONENT_DB      = "database"
COMPONENT_BACKUP  = "backup"

# ── Annotation keys ──────────────────────────────────────────────────────────
# Annotations store metadata that isn't used for selection/filtering.
ANNOTATION_LAST_APPLIED = f"{API_GROUP}/last-applied-hash"   # for idempotency
ANNOTATION_VERSION      = f"{API_GROUP}/operator-version"

OPERATOR_VERSION = "1.0.0"

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_POSTGRES_VERSION  = "15"
DEFAULT_STORAGE           = "5Gi"
DEFAULT_BACKUP_SCHEDULE   = "0 2 * * *"
DEFAULT_BACKUP_BACKEND    = "local"
DEFAULT_BACKUP_RETENTION  = 7          # days
DEFAULT_CPU_REQUEST        = "250m"
DEFAULT_MEMORY_REQUEST     = "256Mi"
DEFAULT_CPU_LIMIT          = "1000m"
DEFAULT_MEMORY_LIMIT       = "512Mi"

# ── Postgres image ────────────────────────────────────────────────────────────
POSTGRES_IMAGE_TEMPLATE = "postgres:{version}-alpine"  # alpine = smaller image

# ── Local backup path (used when backupBackend = local) ───────────────────────
# This path is inside the backup job container.
# In real setup, mount a PVC here so backups survive pod restarts.
LOCAL_BACKUP_PATH = "/backups"

# ── Status phases ─────────────────────────────────────────────────────────────
PHASE_CREATING    = "Creating"
PHASE_RUNNING     = "Running"
PHASE_DEGRADED    = "Degraded"
PHASE_TERMINATING = "Terminating"

# ── Status condition types ────────────────────────────────────────────────────
# Following K8s convention: condition type is a noun describing what's true.
CONDITION_DATABASE_READY    = "DatabaseReady"
CONDITION_BACKUP_CONFIGURED = "BackupConfigured"
CONDITION_STORAGE_READY     = "StorageReady"

# ── Reconciliation ────────────────────────────────────────────────────────────
# How long kopf waits before retrying a failed handler.
# Uses exponential backoff: 10s → 20s → 40s → ... → RETRY_MAX_DELAY
RETRY_INITIAL_DELAY = 10    # seconds
RETRY_MAX_DELAY     = 300   # 5 minutes
RETRY_MAX_ATTEMPTS  = 10


def resource_labels(instance_name: str, component: str) -> dict:
    """
    Standard label set for every resource this operator creates.
    Used in selectors so we can always find what we own.

    Example:
        labels = resource_labels("my-app-db", COMPONENT_DB)
    """
    return {
        LABEL_MANAGED_BY: OPERATOR_NAME,
        LABEL_COMPONENT:  component,
        LABEL_INSTANCE:   instance_name,
        LABEL_PART_OF:    OPERATOR_NAME,
    }


def postgres_image(version: str) -> str:
    """
    Returns the full postgres image string for a given version.

        postgres_image("15") → "postgres:15-alpine"
    """
    return POSTGRES_IMAGE_TEMPLATE.format(version=version)
