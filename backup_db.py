"""
Daily database backup script.

What it does:
1. Runs `pg_dump` against DATABASE_URL to produce a compressed dump file.
2. Uploads that dump to a private Supabase Storage bucket ("db-backups").
3. Deletes backups older than BACKUP_RETENTION_DAYS from that bucket, so
   storage doesn't grow forever.

This is meant to be run daily by the GitHub Actions workflow in
.github/workflows/db-backup.yml, but you can also run it manually:

    DATABASE_URL=... SUPABASE_URL=... SUPABASE_KEY=... python scripts/backup_db.py

Required environment variables:
- DATABASE_URL      : your Neon Postgres connection string
- SUPABASE_URL      : your Supabase project URL
- SUPABASE_KEY      : a Supabase service_role key (needed to write to a
                       private bucket; do NOT use the anon key here)

Optional:
- BACKUP_RETENTION_DAYS : how many days of backups to keep (default 14)
"""

import os
import sys
import subprocess
import datetime

from supabase import create_client

BUCKET_NAME = "db-backups"
RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "14"))


def fail(message: str):
    print(f"❌ {message}", file=sys.stderr)
    sys.exit(1)


def main():
    database_url = os.getenv("DATABASE_URL")
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    if not database_url:
        fail("DATABASE_URL is not set.")
    if not supabase_url or not supabase_key:
        fail("SUPABASE_URL / SUPABASE_KEY are not set.")

    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
    dump_filename = f"backup_{timestamp}.dump"
    dump_path = f"/tmp/{dump_filename}"

    print(f"Running pg_dump -> {dump_path} ...")
    result = subprocess.run(
        ["pg_dump", database_url, "-F", "c", "-f", dump_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"pg_dump failed:\n{result.stderr}")

    dump_size_mb = os.path.getsize(dump_path) / (1024 * 1024)
    print(f"Dump created successfully ({dump_size_mb:.2f} MB).")

    print("Connecting to Supabase Storage...")
    supabase_client = create_client(supabase_url, supabase_key)

    print(f"Uploading {dump_filename} to bucket '{BUCKET_NAME}'...")
    with open(dump_path, "rb") as f:
        supabase_client.storage.from_(BUCKET_NAME).upload(
            path=dump_filename,
            file=f.read(),
            file_options={"content-type": "application/octet-stream"},
        )
    print("Upload complete.")

    print(f"Pruning backups older than {RETENTION_DAYS} days...")
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=RETENTION_DAYS)
    existing_files = supabase_client.storage.from_(BUCKET_NAME).list()

    deleted_count = 0
    for file_info in existing_files:
        name = file_info.get("name", "")
        # Expect names like backup_2026-07-09_020000.dump
        if not (name.startswith("backup_") and name.endswith(".dump")):
            continue
        try:
            date_part = name[len("backup_"):].split(".")[0]  # 2026-07-09_020000
            file_date = datetime.datetime.strptime(date_part, "%Y-%m-%d_%H%M%S")
        except ValueError:
            continue
        if file_date < cutoff:
            supabase_client.storage.from_(BUCKET_NAME).remove([name])
            deleted_count += 1
            print(f"  Deleted old backup: {name}")

    print(f"Pruned {deleted_count} old backup(s).")
    print("✅ Backup complete.")


if __name__ == "__main__":
    main()
