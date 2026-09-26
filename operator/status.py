"""
status.py — Writes status conditions back to K8s objects.

K8S STATUS CONVENTION:
  Every managed resource should have a .status block with:
    phase: high-level state (Creating, Running, Degraded, Terminating)
    message: human-readable explanation
    endpoint: connection string for consumers
    conditions: array of typed conditions (K8s standard)
    observedGeneration: last spec generation reconciled

WHY STATUS SUBRESOURCE EXISTS:
  Without it, a status patch would also increment metadata.generation,
  which would trigger an UPDATE event → reconcile → status patch → loop.
  The status subresource lets us write .status without touching .spec.

INFINITE RECONCILIATION LOOP BUG:
  1. Operator updates status.phase = "Running"
  2. K8s increments metadata.generation
  3. kopf sees MODIFIED event → fires on_update handler
  4. Handler sees no spec change but reconciles anyway
  5. Updates status again → generation increments → loop forever

SOLUTION:
  - Use status subresource (enabled in CRD)
  - Patch only .status, never .metadata
  - Track observedGeneration to skip stale reconciliations
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from config import (
    API_GROUP, API_VERSION,
    PHASE_CREATING, PHASE_RUNNING, PHASE_DEGRADED, PHASE_TERMINATING,
    CONDITION_DATABASE_READY, CONDITION_BACKUP_CONFIGURED, CONDITION_STORAGE_READY,
)

logger = logging.getLogger(__name__)


class StatusManager:
    """
    Manages the .status subresource for a ManagedPostgres instance.

    All methods write to the `patch` dict passed by kopf.
    kopf automatically sends the patch to K8s at the end of the handler.
    """

    def __init__(
        self,
        namespace: str,
        name: str,
        patch: dict,
        current_status: Optional[dict] = None,
    ):
        self.namespace = namespace
        self.name = name
        self.patch = patch
        self.current_status = current_status or {}

        # Ensure status dict exists in patch
        if "status" not in self.patch:
            self.patch["status"] = {}

    def _now(self) -> str:
        """ISO 8601 UTC timestamp."""
        return datetime.now(timezone.utc).isoformat()

    def _set_phase(self, phase: str, message: str):
        self.patch["status"]["phase"] = phase
        self.patch["status"]["message"] = message
        self.patch["status"]["lastTransitionTime"] = self._now()

    def mark_creating(self):
        """Initial state — resources being provisioned."""
        self._set_phase(PHASE_CREATING, "Provisioning resources")

    def mark_running(self, endpoint: str):
        """Healthy state — database accepting connections."""
        self._set_phase(PHASE_RUNNING, "Database is running and healthy")
        self.patch["status"]["endpoint"] = endpoint

    def mark_degraded(self, reason: str, message: str):
        """Something is wrong but operator is still trying."""
        self._set_phase(PHASE_DEGRADED, message)
        self.patch["status"]["degradedReason"] = reason

    def mark_terminating(self):
        """Deletion in progress — owner references handle cleanup."""
        self._set_phase(PHASE_TERMINATING, "Deleting resources")

    def set_condition(
        self,
        condition_type: str,
        status: str,
        reason: str,
        message: str,
    ):
        """
        Set a K8s-style condition in status.conditions.

        Conditions are the standard way to communicate detailed state.
        Each condition has:
          type:   e.g. "DatabaseReady", "BackupConfigured"
          status: "True" | "False" | "Unknown"
          reason: machine-readable camelCase (e.g. "StatefulSetCreated")
          message: human-readable
          lastTransitionTime: when this condition last changed
        """
        conditions = self.patch["status"].get("conditions", [])
        now = self._now()

        # Find existing condition of this type
        existing_idx = None
        for i, c in enumerate(conditions):
            if c.get("type") == condition_type:
                existing_idx = i
                break

        new_condition = {
            "type": condition_type,
            "status": status,
            "reason": reason,
            "message": message,
            "lastTransitionTime": now,
        }

        # Only update transition time if status actually changed
        if existing_idx is not None:
            old_status = conditions[existing_idx].get("status")
            if old_status == status:
                new_condition["lastTransitionTime"] = conditions[existing_idx].get(
                    "lastTransitionTime", now
                )
            conditions[existing_idx] = new_condition
        else:
            conditions.append(new_condition)

        self.patch["status"]["conditions"] = conditions

    def set_observed_generation(self, generation: int):
        """
        Track which spec generation we've reconciled.

        This prevents the infinite loop:
        - User updates spec → generation increments
        - Operator reconciles → sets observedGeneration = new generation
        - Timer fires → sees observedGeneration == current → skips work
        """
        self.patch["status"]["observedGeneration"] = generation

    def set_backup_info(self, last_backup: Optional[str] = None, count: Optional[int] = None):
        """Update backup-related status fields."""
        if last_backup:
            self.patch["status"]["lastBackup"] = last_backup
        if count is not None:
            self.patch["status"]["backupCount"] = count

    def patch_status_via_api(self, api_client: client.CustomObjectsApi):
        """
        Directly patch status via K8s API (bypassing kopf's patch).

        Use this from non-kopf contexts (e.g., backup/restore jobs).
        """
        try:
            api_client.patch_namespaced_custom_object_status(
                group=API_GROUP,
                version=API_VERSION,
                namespace=self.namespace,
                plural="managedpostgres",
                name=self.name,
                body={"status": self.patch["status"]},
            )
            logger.debug(f"Patched status for {self.namespace}/{self.name}")
        except ApiException as e:
            logger.error(f"Failed to patch status: {e}")
            raise


def create_initial_status() -> dict:
    """Return the initial status block for a new ManagedPostgres."""
    return {
        "phase": PHASE_CREATING,
        "message": "Provisioning resources",
        "conditions": [
            {
                "type": CONDITION_DATABASE_READY,
                "status": "False",
                "reason": "Provisioning",
                "message": "Waiting for resources to be created",
                "lastTransitionTime": datetime.now(timezone.utc).isoformat(),
            },
            {
                "type": CONDITION_STORAGE_READY,
                "status": "False",
                "reason": "Provisioning",
                "message": "Waiting for PVC",
                "lastTransitionTime": datetime.now(timezone.utc).isoformat(),
            },
            {
                "type": CONDITION_BACKUP_CONFIGURED,
                "status": "False",
                "reason": "BackupDisabled",
                "message": "backupEnabled is false",
                "lastTransitionTime": datetime.now(timezone.utc).isoformat(),
            },
        ],
        "observedGeneration": 0,
    }