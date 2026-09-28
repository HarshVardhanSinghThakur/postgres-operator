"""
backup.py — Runs inside the CronJob container.

This script is NOT part of the operator pod.
It runs as a separate container, spun up by the CronJob on schedule.

PLUGGABLE BACKEND PATTERN:
  We define a BackupBackend abstract base class.
  LocalBackend and S3Backend implement it.
  The script reads BACKEND env var and picks the right one.

  Adding a new backend (e.g. GCS, Azure Blob) = write one class.
  The rest of the script doesn't change.

  This is the "don't reinvent the wheel AND stay extensible" balance.

FLOW:
  1. Read config from env vars (injected by CronJob)
  2. Run pg_dump → compressed .sql.gz file
  3. Upload to backend (local PVC path or S3)
  4. Delete old backups beyond retention window
  5. Exit 0 = success, exit 1 = failure (CronJob will retry)

ENV VARS (set by the CronJob in provisioner.py):
  POSTGRES_HOST     - service DNS name
  POSTGRES_PASSWORD - from Secret
  DB_NAME           - database name
  NAMESPACE         - for S3 path prefix
  BACKEND           - "local" or "s3"
  RETENTION_DAYS    - how many days to keep (default 7)
  S3_BUCKET         - required if BACKEND=s3
  S3_REGION         - required if BACKEND=s3
  AWS_ACCESS_KEY_ID     - required if BACKEND=s3
  AWS_SECRET_ACCESS_KEY - required if BACKEND=s3
"""

import gzip
import hashlib
import logging
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# ── Config from environment ───────────────────────────────────────────────────

class BackupConfig:
    def __init__(self):
        self.host            = os.environ["POSTGRES_HOST"]
        self.password        = os.environ["POSTGRES_PASSWORD"]
        self.db_name         = os.environ["DB_NAME"]
        self.namespace       = os.environ.get("NAMESPACE", "default")
        self.backend         = os.environ.get("BACKEND", "local")
        self.retention_days  = int(os.environ.get("RETENTION_DAYS", "7"))

        # S3 config — only needed if backend=s3
        self.s3_bucket  = os.environ.get("S3_BUCKET", "")
        self.s3_region  = os.environ.get("S3_REGION", "us-east-1")

        self.timestamp  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.filename   = f"{self.db_name}-{self.timestamp}.sql.gz"

    def validate(self):
        if self.backend == "s3" and not self.s3_bucket:
            raise ValueError("BACKEND=s3 requires S3_BUCKET to be set")


# ── Abstract backend ──────────────────────────────────────────────────────────

class BackupBackend(ABC):
    """
    Every backend must implement these two methods.
    The main backup flow calls them without knowing which backend is active.
    """

    @abstractmethod
    def upload(self, local_path: Path, config: BackupConfig) -> str:
        """
        Upload the backup file. Returns a reference string (path or URL).
        Raises on failure.
        """
        ...

    @abstractmethod
    def list_backups(self, config: BackupConfig) -> list[str]:
        """
        Return list of backup filenames for this database, oldest first.
        """
        ...

    @abstractmethod
    def delete(self, filename: str, config: BackupConfig):
        """Delete a backup file by filename."""
        ...


# ── Local backend ─────────────────────────────────────────────────────────────

class LocalBackend(BackupBackend):
    """
    Saves backups to a local directory inside the container.

    In a real cluster this directory should be a mounted PVC
    so backups survive pod restarts. Without a PVC, backups
    are lost when the backup pod terminates.

    Local path structure:
      /backups/<db_name>/<timestamp>.sql.gz
    """

    BASE_PATH = Path(os.environ.get("LOCAL_BACKUP_PATH", "/backups"))

    def _db_path(self, config: BackupConfig) -> Path:
        path = self.BASE_PATH / config.db_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def upload(self, local_path: Path, config: BackupConfig) -> str:
        dest = self._db_path(config) / config.filename
        shutil.copy2(local_path, dest)
        logger.info(f"Local backup saved: {dest}")
        return str(dest)

    def list_backups(self, config: BackupConfig) -> list[str]:
        db_path = self._db_path(config)
        files   = sorted(db_path.glob("*.sql.gz"))
        return [f.name for f in files]

    def delete(self, filename: str, config: BackupConfig):
        path = self._db_path(config) / filename
        path.unlink(missing_ok=True)
        logger.info(f"Deleted old backup: {filename}")


# ── S3 backend ────────────────────────────────────────────────────────────────

class S3Backend(BackupBackend):
    """
    Saves backups to AWS S3.

    S3 path structure:
      s3://<bucket>/<namespace>/<db_name>/<timestamp>.sql.gz

    boto3 is only imported here — if BACKEND=local, boto3 doesn't
    even need to be installed. This is the pluggable pattern payoff.
    """

    def __init__(self):
        try:
            import boto3
            self.s3 = boto3.client("s3")
        except ImportError:
            raise RuntimeError(
                "boto3 is not installed. Install it or use BACKEND=local"
            )

    def _s3_key(self, filename: str, config: BackupConfig) -> str:
        return f"{config.namespace}/{config.db_name}/{filename}"

    def upload(self, local_path: Path, config: BackupConfig) -> str:
        key = self._s3_key(config.filename, config)
        self.s3.upload_file(
            str(local_path),
            config.s3_bucket,
            key,
            ExtraArgs={"ServerSideEncryption": "AES256"},  # encrypt at rest
        )
        s3_url = f"s3://{config.s3_bucket}/{key}"
        logger.info(f"S3 backup uploaded: {s3_url}")
        return s3_url

    def list_backups(self, config: BackupConfig) -> list[str]:
        prefix   = f"{config.namespace}/{config.db_name}/"
        response = self.s3.list_objects_v2(Bucket=config.s3_bucket, Prefix=prefix)
        objects  = response.get("Contents", [])
        # Sort by LastModified, oldest first
        objects.sort(key=lambda o: o["LastModified"])
        # Return just the filename part (strip prefix)
        return [obj["Key"].replace(prefix, "") for obj in objects]

    def delete(self, filename: str, config: BackupConfig):
        key = self._s3_key(filename, config)
        self.s3.delete_object(Bucket=config.s3_bucket, Key=key)
        logger.info(f"Deleted old S3 backup: {key}")


# ── Backend factory ───────────────────────────────────────────────────────────

def get_backend(config: BackupConfig) -> BackupBackend:
    """
    Return the right backend based on BACKEND env var.
    Adding a new backend: add an elif here + write the class.
    Nothing else changes.
    """
    if config.backend == "s3":
        return S3Backend()
    elif config.backend == "local":
        return LocalBackend()
    else:
        raise ValueError(f"Unknown BACKEND: {config.backend}. Use 'local' or 's3'")


# ── pg_dump ───────────────────────────────────────────────────────────────────

def run_pg_dump(config: BackupConfig, output_path: Path):
    """
    Run pg_dump and write compressed output to output_path.

    pg_dump flags:
      -h  host
      -U  user
      -d  database name
      -F p  plain SQL format (most portable, works with psql restore)
      --no-password  use PGPASSWORD env var instead of interactive prompt

    We pipe stdout directly into gzip to avoid storing uncompressed SQL.
    This matters for large databases.
    """
    env = os.environ.copy()
    env["PGPASSWORD"] = config.password   # pg_dump reads this automatically

    pg_dump_cmd = [
        "pg_dump",
        "-h", config.host,
        "-U", "postgres",
        "-d", config.db_name,
        "-F", "p",         # plain SQL
        "--no-password",
    ]

    logger.info(f"Running pg_dump: {' '.join(pg_dump_cmd)}")

    with gzip.open(output_path, "wb") as gz_file:
        result = subprocess.run(
            pg_dump_cmd,
            stdout=gz_file,
            stderr=subprocess.PIPE,
            env=env,
            timeout=3600,    # 1 hour max — for large databases
        )

    if result.returncode != 0:
        error = result.stderr.decode()
        raise RuntimeError(f"pg_dump failed (exit {result.returncode}): {error}")

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(f"pg_dump complete: {output_path} ({size_mb:.1f} MB)")


# ── Retention cleanup ─────────────────────────────────────────────────────────

def apply_retention(backend: BackupBackend, config: BackupConfig):
    """
    Delete backups older than retention_days.

    We parse the timestamp from the filename.
    Filenames format: <dbname>-<YYYYMMDDTHHMMSSz>.sql.gz
    """
    backups    = backend.list_backups(config)
    cutoff     = datetime.now(timezone.utc) - timedelta(days=config.retention_days)
    deleted    = 0

    for filename in backups:
        try:
            # Extract timestamp from filename: mydb-20260901T020000Z.sql.gz
            ts_str = filename.replace(f"{config.db_name}-", "").replace(".sql.gz", "")
            ts     = datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if ts < cutoff:
                backend.delete(filename, config)
                deleted += 1
        except (ValueError, IndexError):
            logger.warning(f"Could not parse timestamp from filename: {filename}")

    remaining = len(backups) - deleted
    logger.info(f"Retention: deleted {deleted} old backups, {remaining} remaining")
    return remaining


def create_receipt(config: BackupConfig, dump_path: Path, backend: BackupBackend):
    """
    Create a receipt file with row count and checksum of the backup.
    For local backend: writes receipt.txt to the backup directory.
    For S3 backend: uploads receipt.txt to S3 alongside the backup.
    """
    try:
        # Compute SHA256 checksum of the compressed dump
        sha256_hash = hashlib.sha256()
        with open(dump_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        checksum = sha256_hash.hexdigest()

        # Count rows (INSERT statements) in the dump
        # Decompress and count INSERT lines
        row_count = 0
        import gzip as gz
        with gz.open(dump_path, "rt", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("INSERT"):
                    row_count += 1

        receipt_content = f"rows={row_count} checksum={checksum}\n"

        if config.backend == "local":
            # Write receipt to the backup directory
            db_path = Path(os.environ.get("LOCAL_BACKUP_PATH", "/backups")) / config.db_name
            receipt_path = db_path / "receipt.txt"
            with open(receipt_path, "w") as f:
                f.write(receipt_content)
            logger.info(f"Receipt created: {receipt_path} (rows={row_count})")
        elif config.backend == "s3":
            # Upload receipt to S3
            import boto3
            s3 = boto3.client("s3", region_name=config.s3_region)
            key = f"{config.namespace}/{config.db_name}/receipt.txt"
            s3.put_object(
                Bucket=config.s3_bucket,
                Key=key,
                Body=receipt_content.encode(),
                ServerSideEncryption="AES256",
            )
            logger.info(f"Receipt uploaded to S3: s3://{config.s3_bucket}/{key}")

    except Exception as e:
        logger.warning(f"Failed to create receipt (non-fatal): {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config = BackupConfig()

    try:
        config.validate()
    except ValueError as e:
        logger.error(f"Config error: {e}")
        sys.exit(1)

    logger.info(
        f"Starting backup: db={config.db_name} "
        f"backend={config.backend} "
        f"retention={config.retention_days}d"
    )

    backend    = get_backend(config)
    tmp_path   = Path(f"/tmp/{config.filename}")

    try:
        # Step 1: dump + compress
        run_pg_dump(config, tmp_path)

        # Step 2: upload to backend
        reference = backend.upload(tmp_path, config)
        logger.info(f"Backup stored at: {reference}")

        # Step 3: create receipt (row count + checksum)
        create_receipt(config, tmp_path, backend)

        # Step 4: enforce retention policy
        remaining = apply_retention(backend, config)

        logger.info(
            f"Backup complete: {config.filename} "
            f"({remaining} backups retained)"
        )
        sys.exit(0)

    except Exception as e:
        logger.error(f"Backup FAILED: {e}", exc_info=True)
        sys.exit(1)    # CronJob sees non-zero exit → marks job Failed → retries

    finally:
        # Always clean up temp file
        if tmp_path.exists():
            tmp_path.unlink()


if __name__ == "__main__":
    main()
