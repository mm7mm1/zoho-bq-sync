#!/usr/bin/env bash
# Build the image with Cloud Build and deploy/update the Cloud Run Job.
# Usage (after: set -a; source .env; set +a): bash deploy/deploy_job.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/env.sh

gcloud builds submit --tag "$IMAGE" --project "$PROJECT_ID" .

# The secret is mounted as a FILE and read inside the command handler, so rotating it
# (new secret version) needs no code change and never appears in env dumps.
gcloud run jobs deploy "$JOB_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$RUNTIME_SA" \
  --tasks 1 \
  --parallelism 1 \
  --max-retries 1 \
  --task-timeout 3600s \
  --cpu 1 \
  --memory 1Gi \
  --args sync \
  --set-secrets "/secrets/zoho/oauth.json=${SECRET_NAME}:latest" \
  --set-env-vars "^|^ZOHO_OAUTH_JSON_FILE=/secrets/zoho/oauth.json|ZOHO_DC=${ZOHO_DC:?ZOHO_DC is not set, source .env first}|ZOHO_MODULES=Wedding_Bookings|TABLE_NAME_OVERRIDES=Booked_Services=wedding_bookings_services|GCP_PROJECT=${PROJECT_ID}|BQ_DATASET=${BQ_DATASET}|BQ_STAGING_DATASET=${BQ_STAGING_DATASET}|BQ_LOCATION=${BQ_LOCATION}|LOG_FORMAT=json|ZOHO_MAX_API_CALLS_PER_RUN=2000"

# Optional companion job: fails (exit 2) when data is stale, which triggers the job-failure alert.
gcloud run jobs deploy "${JOB_NAME}-freshness" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$RUNTIME_SA" \
  --tasks 1 --max-retries 0 --task-timeout 300s \
  --args freshness,--max-age-hours,26 \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},BQ_DATASET=${BQ_DATASET},BQ_LOCATION=${BQ_LOCATION},LOG_FORMAT=json"

echo "Deployed ${JOB_NAME} with image ${IMAGE}"
