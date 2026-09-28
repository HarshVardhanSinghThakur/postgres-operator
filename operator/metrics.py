"""
metrics.py — Prometheus metrics for the operator.

WHY THIS EXISTS:
  Without metrics you are flying blind. When something breaks in production
  you want to know: how many instances are running? how many reconciliations
  failed? when did the last backup succeed?

  Prometheus scrapes the /metrics endpoint exposed by this module.
  Grafana visualizes it. Alerts fire when things go wrong.

HOW PROMETHEUS WORKS (quick primer):
  - Your app exposes a /metrics HTTP endpoint
  - Prometheus scrapes it every N seconds (configured in prometheus.yml)
  - Each metric has a TYPE (Counter, Gauge, Histogram) and optional labels

  Counter  → only goes up. total requests, total errors, total backups.
  Gauge    → goes up and down. number of running instances, queue depth.
  Histogram→ tracks distributions. how long did reconciliation take?

USAGE:
  from metrics import METRICS
  METRICS.instances_total.labels(namespace="default").inc()
"""

from prometheus_client import Counter, Gauge, Histogram, start_http_server
import logging

logger = logging.getLogger(__name__)

# ── Port where /metrics is exposed ───────────────────────────────────────────
METRICS_PORT = 8080


class OperatorMetrics:
    """
    All Prometheus metrics in one place.

    Grouped by what they measure:
      1. Instance lifecycle   — how many databases exist, in what state
      2. Reconciliation       — control loop health
      3. Backup               — backup success/failure tracking
      4. K8s API calls        — how often we talk to the API server
    """

    def __init__(self):
        # ── 1. Instance lifecycle ─────────────────────────────────────────────

        # Gauge: current number of ManagedPostgres instances per namespace+phase
        # Goes up when created, down when deleted.
        # Label 'phase' lets us see: 3 Running, 1 Creating, 0 Degraded
        self.instances_total = Gauge(
            "postgres_operator_instances_total",
            "Number of ManagedPostgres instances currently managed",
            labelnames=["namespace", "phase"],
        )

        # Counter: total create events processed (never decreases)
        self.instances_created = Counter(
            "postgres_operator_instances_created_total",
            "Total ManagedPostgres instances created since operator start",
            labelnames=["namespace"],
        )

        # Counter: total delete events processed
        self.instances_deleted = Counter(
            "postgres_operator_instances_deleted_total",
            "Total ManagedPostgres instances deleted since operator start",
            labelnames=["namespace"],
        )

        # ── 2. Reconciliation ─────────────────────────────────────────────────

        # Counter: every time reconcile runs (whether it does work or not)
        self.reconcile_total = Counter(
            "postgres_operator_reconcile_total",
            "Total reconciliation loops executed",
            labelnames=["namespace", "result"],  # result: success | failure | noop
        )

        # Histogram: how long each reconciliation takes
        # Buckets = the time ranges we care about (seconds)
        # Lets us answer: "95% of reconciliations finish in under X seconds"
        self.reconcile_duration = Histogram(
            "postgres_operator_reconcile_duration_seconds",
            "Time taken for a single reconciliation loop",
            labelnames=["namespace"],
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
        )

        # Counter: reconciliations that ended in an error
        self.reconcile_errors = Counter(
            "postgres_operator_reconcile_errors_total",
            "Total reconciliation errors",
            labelnames=["namespace", "error_type"],
        )

        # ── 3. Backup ─────────────────────────────────────────────────────────

        # Counter: successful backups
        self.backups_success = Counter(
            "postgres_operator_backups_success_total",
            "Total successful backups",
            labelnames=["namespace", "instance", "backend"],  # backend: local | s3
        )

        # Counter: failed backups
        self.backups_failed = Counter(
            "postgres_operator_backups_failed_total",
            "Total failed backups",
            labelnames=["namespace", "instance", "backend"],
        )

        # Gauge: Unix timestamp of last successful backup per instance
        # Alertmanager rule: alert if now() - last_backup > 25h (missed a day)
        self.last_backup_timestamp = Gauge(
            "postgres_operator_last_backup_timestamp_seconds",
            "Unix timestamp of last successful backup",
            labelnames=["namespace", "instance"],
        )

        # Gauge: how many backup files exist for each instance
        self.backup_count = Gauge(
            "postgres_operator_backup_files_total",
            "Number of backup files currently stored",
            labelnames=["namespace", "instance"],
        )

        # ── 4. K8s API calls ──────────────────────────────────────────────────

        # Counter: every call to the K8s API server, labelled by what we did
        # Lets us spot if we're hammering the API too hard
        self.k8s_api_calls = Counter(
            "postgres_operator_k8s_api_calls_total",
            "Total Kubernetes API calls made by the operator",
            labelnames=["operation", "resource", "result"],
            # operation: create | patch | delete | get
            # resource: statefulset | service | secret | pvc | cronjob
            # result: success | failure
        )

        # ── 5. Restore ──────────────────────────────────────────────────────────

        # Counter: restore operations started
        self.restores_started = Counter(
            "postgres_operator_restores_started_total",
            "Total restore operations started",
            labelnames=["namespace"],
        )

        # Counter: restore operations completed successfully
        self.restores_completed = Counter(
            "postgres_operator_restores_completed_total",
            "Total restore operations completed successfully",
            labelnames=["namespace"],
        )

        # Counter: restore operations failed
        self.restores_failed = Counter(
            "postgres_operator_restores_failed_total",
            "Total restore operations failed",
            labelnames=["namespace"],
        )

        # Histogram: restore duration
        self.restore_duration = Histogram(
            "postgres_operator_restore_duration_seconds",
            "Time taken for a restore operation",
            labelnames=["namespace"],
            buckets=[10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0],
        )

        # Histogram: recovery duration (time from data loss detection to recovery complete)
        self.restores_duration_seconds = Histogram(
            "postgres_operator_recovery_duration_seconds",
            "Time taken for a full recovery (data loss to restore complete)",
            labelnames=["namespace"],
            buckets=[10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0],
        )

    def start_metrics_server(self):
        """
        Start the HTTP server that exposes /metrics.
        Prometheus scrapes this endpoint.

        Call this once at operator startup.
        """
        try:
            start_http_server(METRICS_PORT)
            logger.info(f"Prometheus metrics available at :{METRICS_PORT}/metrics")
        except OSError as e:
            # Port already in use — harmless during local dev restarts
            logger.warning(f"Could not start metrics server: {e}")


# ── Singleton ─────────────────────────────────────────────────────────────────
# Import this anywhere: from metrics import METRICS
# All handlers use the same metric objects → counters accumulate correctly.
METRICS = OperatorMetrics()
