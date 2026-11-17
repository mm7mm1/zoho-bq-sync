# Deployment to Google Cloud

Status: scripts are provided and syntax-reviewed, but **nothing has been deployed** from this
repository yet. Run the steps below in your own project.

## Identities and permissions (least privilege)

| Identity | Role | Scope | Why |
|---|---|---|---|
| `zoho-bq-sync@` (runtime) | `roles/bigquery.jobUser` | project | run load and query jobs |
| | `roles/bigquery.dataEditor` | datasets `zoho_crm`, `zoho_crm_staging` only | create, alter and merge tables |
| | `roles/secretmanager.secretAccessor` | secret `zoho-oauth` only | read the OAuth JSON |
| `zoho-bq-sync-scheduler@` | `roles/run.invoker` | the two jobs only | start executions |

Datasets are created once by `setup_gcp.sh`, so the runtime SA needs no project-wide
`bigquery.datasets.create`. Cloud Run sends stdout to Cloud Logging without extra roles.

## Secret handling

- One secret `zoho-oauth` holds `{"client_id","client_secret","refresh_token","accounts_url","api_domain"}`.
- It is mounted as a file (`--set-secrets=/secrets/zoho/oauth.json=zoho-oauth:latest`) and read
  inside the command handler. It never sits in env vars or image layers.
- Rotation: add a new secret version and the next execution picks it up. Zoho refresh tokens do
  not rotate on use, so nothing is written back.

## Steps

```bash
set -a; source .env; set +a                          # GCP_PROJECT plus this repo's gcloud config and ADC
source deploy/env.sh                                 # refuses to continue if the active gcloud project differs
bash deploy/setup_gcp.sh ./secrets/zoho-oauth.json   # APIs, SAs, IAM, datasets, secret
bash deploy/deploy_job.sh                            # Cloud Build image + Cloud Run Jobs
bash deploy/scheduler.sh                             # daily 02:00 sync, 08:00 freshness
bash deploy/monitoring.sh                            # log-based metrics
```

Manual run and logs:

```bash
gcloud run jobs execute zoho-bq-sync --project "$PROJECT_ID" --region "$REGION" --wait
gcloud logging read --project "$PROJECT_ID" 'resource.type="cloud_run_job" AND resource.labels.job_name="zoho-bq-sync" AND jsonPayload.event:*' --limit 50 --format='value(jsonPayload.event,jsonPayload.module,jsonPayload.message)'
```

Manual full resync, overriding the args for a single execution:

```bash
gcloud run jobs execute zoho-bq-sync --project "$PROJECT_ID" --region "$REGION" --args sync,--full --wait
```

The declarative alternative to `deploy_job.sh` is
`envsubst < deploy/cloudrun-job.yaml > /tmp/job.yaml && gcloud run jobs replace /tmp/job.yaml --project "$PROJECT_ID" --region "$REGION"` (after `source deploy/env.sh`).

## Runtime settings

- `--tasks 1 --parallelism 1`. Zoho concurrency is per app, and sequential modules keep it at 1.
- `--max-retries 1`. A retried task is safe because writes are idempotent and the checkpoint is
  transactional.
- `--task-timeout 3600s`. Increase it for big initial loads, or use Bulk Read (see zoho_api_notes.md).
- Overlapping executions (a manual run during the scheduled one) are safe. Concurrent
  transactions on the same table abort one side, which is retried. Versioned MERGE prevents
  stale overwrites.

## Logging and freshness monitoring

| Signal | Source | Alert |
|---|---|---|
| Job execution failed | `run.googleapis.com/job/completed_execution_count{result="failed"}` | page, any failure |
| Module failed (table untouched) | log metric `zoho_bq_sync_module_failures` (`event=module_sync_failed`) | > 0 |
| Data stale | `zoho-bq-sync-freshness` job exits 2 when `_sync_state.updated_at` is older than 26 h, which counts as a failed execution | same alert |
| Reconciliation mismatch | log metric `zoho_bq_sync_reconciliation_mismatch` | > 0 for 2 consecutive days |
| Rate limiting | log metric `zoho_bq_sync_rate_limited` | trend |
| Schema changed | log metric `zoho_bq_sync_schema_changes` | informational |

Useful log filters (all lines are JSON with `event`, `run_id`, `module`):

```
jsonPayload.run_id="r20261117T020000Z_ab12cd"
jsonPayload.event="schema_evolved"
jsonPayload.event="pagination_drift_detected"
severity>=WARNING
```

SQL views of the same information are in `sql/monitoring/`.
