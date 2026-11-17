#!/usr/bin/env bash
# Daily trigger for the sync job (02:00 Europe/Kyiv) and the freshness check (08:00).
# Usage (after: set -a; source .env; set +a): bash deploy/scheduler.sh
set -euo pipefail
source "$(dirname "$0")/env.sh"

# The scheduler identity may only run these two jobs.
for job in "$JOB_NAME" "${JOB_NAME}-freshness"; do
  gcloud run jobs add-iam-policy-binding "$job" --project "$PROJECT_ID" --region "$REGION" \
    --member="serviceAccount:${SCHEDULER_SA}" --role="roles/run.invoker"
done

create_or_update() {
  local name="$1" schedule="$2" job="$3"
  local uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${job}:run"
  if gcloud scheduler jobs describe "$name" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
    verb=update
  else
    verb=create
  fi
  gcloud scheduler jobs "$verb" http "$name" \
    --project "$PROJECT_ID" \
    --location "$REGION" \
    --schedule "$schedule" \
    --time-zone "Europe/Kyiv" \
    --uri "$uri" \
    --http-method POST \
    --oauth-service-account-email "$SCHEDULER_SA" \
    --attempt-deadline 180s
}

create_or_update "${JOB_NAME}-daily" "0 2 * * *" "$JOB_NAME"
create_or_update "${JOB_NAME}-freshness-daily" "0 8 * * *" "${JOB_NAME}-freshness"
