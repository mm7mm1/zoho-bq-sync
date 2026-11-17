# Shared deployment variables. Every deploy script sources this file itself, so the checks
# below run before any gcloud/bq call. Load the repo .env first: set -a; source .env; set +a

# The project comes only from GCP_PROJECT (the variable the pipeline reads). No placeholder
# default and no inherited PROJECT_ID: a guessed or leftover ID could be someone else's project.
if [ -z "${GCP_PROJECT:-}" ]; then
  echo "GCP_PROJECT is not set. Run: set -a; source .env; set +a" >&2
  return 1 2>/dev/null || exit 1
fi
export PROJECT_ID="$GCP_PROJECT"

# Every command passes --project, but runs as the active gcloud account. If the active
# configuration points at another project, it is almost certainly another account's
# configuration too, so stop before anything is created or changed.
_active_project="$(gcloud config get-value project 2>/dev/null)"
if [ "$_active_project" != "$PROJECT_ID" ]; then
  echo "Active gcloud project is '${_active_project:-unset}', expected '${PROJECT_ID}'." >&2
  echo "Set CLOUDSDK_ACTIVE_CONFIG_NAME in .env to the configuration for ${PROJECT_ID}." >&2
  return 1 2>/dev/null || exit 1
fi
echo "Target project ${PROJECT_ID} as $(gcloud config get-value account 2>/dev/null)" >&2
unset _active_project

export REGION="${REGION:-europe-west1}"
export BQ_LOCATION="${BQ_LOCATION:-EU}"
export BQ_DATASET="${BQ_DATASET:-zoho_crm}"
export BQ_STAGING_DATASET="${BQ_STAGING_DATASET:-zoho_crm_staging}"
export AR_REPO="${AR_REPO:-zoho-bq-sync}"
export JOB_NAME="${JOB_NAME:-zoho-bq-sync}"
export RUNTIME_SA="zoho-bq-sync@${PROJECT_ID}.iam.gserviceaccount.com"
export SCHEDULER_SA="zoho-bq-sync-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
export SECRET_NAME="${SECRET_NAME:-zoho-oauth}"
export IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/zoho-bq-sync:${IMAGE_TAG}"
