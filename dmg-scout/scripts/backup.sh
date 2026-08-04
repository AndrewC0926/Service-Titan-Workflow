#!/usr/bin/env bash
# Nightly Postgres backup with 30-day retention to S3-compatible storage.
# Required env: DATABASE_URL, BACKUP_S3_URI (e.g. s3://dmg-scout-backups),
# and AWS credentials (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_ENDPOINT_URL
# for Cloudflare R2 or Backblaze B2).
#
# Restore (tested procedure — see RUNBOOK.md):
#   aws s3 cp "$BACKUP_S3_URI/scout-YYYY-MM-DD.dump" /tmp/restore.dump
#   pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" /tmp/restore.dump
set -euo pipefail

STAMP=$(date -u +%Y-%m-%d)
OUT="/tmp/scout-${STAMP}.dump"

pg_dump --format=custom --no-owner --file="$OUT" "$DATABASE_URL"
aws s3 cp "$OUT" "${BACKUP_S3_URI}/scout-${STAMP}.dump"
rm -f "$OUT"

# 30-day retention
CUTOFF=$(date -u -d "30 days ago" +%Y-%m-%d 2>/dev/null || date -u -v-30d +%Y-%m-%d)
aws s3 ls "${BACKUP_S3_URI}/" | awk '{print $4}' | while read -r key; do
  [ -z "$key" ] && continue
  file_date=$(echo "$key" | sed -n 's/^scout-\([0-9-]*\)\.dump$/\1/p')
  if [ -n "$file_date" ] && [[ "$file_date" < "$CUTOFF" ]]; then
    echo "pruning $key (older than $CUTOFF)"
    aws s3 rm "${BACKUP_S3_URI}/${key}"
  fi
done

echo "backup complete: scout-${STAMP}.dump"
# Optional separate dead man's switch for backups:
[ -n "${BACKUP_HEALTHCHECK_URL:-}" ] && curl -fsS "$BACKUP_HEALTHCHECK_URL" >/dev/null || true
