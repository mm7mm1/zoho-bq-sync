#!/usr/bin/env bash
# Log-based metrics for alerting. Attach alert policies to them in Cloud Monitoring
# (UI or `gcloud alpha monitoring policies create --policy-from-file`).
# Usage (after: set -a; source .env; set +a): bash deploy/monitoring.sh
set -euo pipefail
source "$(dirname "$0")/env.sh"

BASE='resource.type="cloud_run_job" AND resource.labels.job_name="'"$JOB_NAME"'"'

gcloud logging metrics create zoho_bq_sync_module_failures --project "$PROJECT_ID" \
  --description="Modules that failed to sync (data and checkpoint untouched)" \
  --log-filter="${BASE} AND jsonPayload.event=\"module_sync_failed\"" 2>/dev/null || echo "metric exists"

gcloud logging metrics create zoho_bq_sync_reconciliation_mismatch --project "$PROJECT_ID" \
  --description="Row-count reconciliation mismatches" \
  --log-filter="${BASE} AND jsonPayload.event=\"reconciliation_mismatch\"" 2>/dev/null || echo "metric exists"

gcloud logging metrics create zoho_bq_sync_schema_changes --project "$PROJECT_ID" \
  --description="Columns added automatically (informational)" \
  --log-filter="${BASE} AND jsonPayload.event=\"schema_evolved\"" 2>/dev/null || echo "metric exists"

gcloud logging metrics create zoho_bq_sync_rate_limited --project "$PROJECT_ID" \
  --description="Zoho 429 retries (approaching concurrency or credit limits)" \
  --log-filter="${BASE} AND jsonPayload.event=\"zoho_retry\" AND jsonPayload.reason:\"429\"" 2>/dev/null || echo "metric exists"

echo "Also alert on metric run.googleapis.com/job/completed_execution_count with result=\"failed\"."
