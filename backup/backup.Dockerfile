# ── Backup Container Dockerfile ──────────────────────────────────────────────
#
# This image runs as a CronJob inside the cluster.
# It needs: Python, pg_dump (from postgresql-client), and optional boto3.
#
# pg_dump is NOT in the operator image — separation of concerns.
# The operator image knows Kubernetes. The backup image knows Postgres.

FROM python:3.11-alpine

# postgresql-client gives us pg_dump and psql
# Pinned to 16, the newest server major the CRD allows (14/15/16). The unversioned "postgresql-client" floats to
# the newest release (pg_dump 18), whose dumps contain settings older servers reject ("transaction_timeout").
RUN apk add --no-cache postgresql16-client

WORKDIR /app

# Install Python deps
# boto3 is included but only used if BACKEND=s3
# If you never use S3, this adds ~10MB — acceptable tradeoff
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backup.py .

# Non-root user
RUN addgroup -S backup && adduser -S backup -G backup
USER backup

# Default local backup path — mount a PVC here in production
ENV LOCAL_BACKUP_PATH=/backups

CMD ["python", "backup.py"]
