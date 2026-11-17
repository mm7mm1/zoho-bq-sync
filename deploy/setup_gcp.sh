#!/usr/bin/env bash
# One-time project setup: APIs, service accounts, least-privilege IAM, datasets, secret.
# Usage (after: set -a; source .env; set +a): bash deploy/setup_gcp.sh ./secrets/zoho-oauth.json
set -euo pipefail
source "$(dirname "$0")/env.sh"
OAUTH_JSON="${1:?path to zoho-oauth.json produced by scripts/exchange_grant_token.py}"

gcloud services enable \
  run.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com \
  bigquery.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  logging.googleapis.com monitoring.googleapis.com --project "$PROJECT_ID"

gcloud artifacts repositories create "$AR_REPO" --repository-format=docker \
  --location="$REGION" --project "$PROJECT_ID" 2>/dev/null || echo "repository exists"

# Runtime identity of the Cloud Run Job.
gcloud iam service-accounts create zoho-bq-sync --display-name="Zoho -> BigQuery sync" \
  --project "$PROJECT_ID" 2>/dev/null || echo "runtime SA exists"
# Identity Cloud Scheduler uses to start the job.
gcloud iam service-accounts create zoho-bq-sync-scheduler --display-name="Zoho sync scheduler" \
  --project "$PROJECT_ID" 2>/dev/null || echo "scheduler SA exists"

# Datasets are created here so the runtime SA needs no project-wide dataset-create permission.
bq --project_id="$PROJECT_ID" --location="$BQ_LOCATION" mk --dataset \
  --description "Zoho CRM replica (zoho-bq-sync)" "${PROJECT_ID}:${BQ_DATASET}" 2>/dev/null || echo "dataset exists"
bq --project_id="$PROJECT_ID" --location="$BQ_LOCATION" mk --dataset \
  --default_table_expiration 86400 --description "zoho-bq-sync staging (auto-expiring)" \
  "${PROJECT_ID}:${BQ_STAGING_DATASET}" 2>/dev/null || echo "staging dataset exists"

# BigQuery: run jobs in the project, edit tables only in the two datasets.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/bigquery.jobUser" --condition=None
for ds in "$BQ_DATASET" "$BQ_STAGING_DATASET"; do
  bq query --project_id="$PROJECT_ID" --location="$BQ_LOCATION" --use_legacy_sql=false \
    "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`${PROJECT_ID}.${ds}\` TO 'serviceAccount:${RUNTIME_SA}'"
done

# Secret: the whole OAuth client as one JSON value, readable only by the runtime SA.
gcloud secrets create "$SECRET_NAME" --replication-policy=automatic --project "$PROJECT_ID" 2>/dev/null || echo "secret exists"
gcloud secrets versions add "$SECRET_NAME" --data-file="$OAUTH_JSON" --project "$PROJECT_ID"
gcloud secrets add-iam-policy-binding "$SECRET_NAME" --project "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor"

echo "Done. Delete the local copy now: rm -P \"$OAUTH_JSON\" (macOS) or shred -u \"$OAUTH_JSON\" (Linux)"
