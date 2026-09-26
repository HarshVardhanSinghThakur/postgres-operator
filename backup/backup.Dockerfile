# ── Backup Container Dockerfile ──────────────────────────────────────────────
#
# This image runs as a CronJob inside the cluster.
# It needs: Python, pg_dump (from postgresql-client), and optional boto3.
#
# pg_dump is NOT in the operator image — separation of concerns.
# The operator image knows Kubernetes. The backup image knows Postgres.

FROM python:3.11-alpine

# postgresql-client gives us pg_dump and psql
RUN apk add --no-cache postgresql-client

WORKDIR /app

# Install Python deps
# boto3 is included but only used if BACKEND=s3
# If you never use S3, this adds ~10MB — acceptable tradeoff
COPY backup/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backup/backup.py .

# Non-root user
RUN addgroup -S backup && adduser -S backup -G backup
USER backup

# Default local backup path — mount a PVC here in production
ENV LOCAL_BACKUP_PATH=/backups

CMD ["python", "backup.py"]
