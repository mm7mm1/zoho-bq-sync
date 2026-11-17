# 15-minute live demo

Everything shown is live: a Zoho developer org with **synthetic** bookings (`WB-SYN-` prefix)
and a real BigQuery dataset. Mocks appear only when the unit tests are shown, and the demo
says so.

## A. Setup checklist (day before)

1. Python environment

```bash
cd zoho-bq-sync && python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && pip install -e .
```
Expected: installs without errors.

2. Unit tests

```bash
python -m pytest tests -q
```
Expected: `132 passed`.

3. Zoho module, subform and OAuth, following [zoho_setup.md](zoho_setup.md)

```bash
python scripts/exchange_grant_token.py --dc us-developer --code 1000.<grant-code> --out secrets/zoho-oauth.json
```
Expected: `Wrote secrets/zoho-oauth.json (scopes: ...)`.

4. Local configuration

```bash
cp .env.example .env    # then set GCP_PROJECT: it has no default on purpose
set -a && source .env && set +a
```
Expected: `GCP_PROJECT` is set to your demo project, `LOG_FORMAT=text`.

5. Google credentials for BigQuery, kept apart from any other gcloud work on this machine

```bash
gcloud auth login you@example.com    # only if the account is not in `gcloud auth list` yet
gcloud config configurations create zoho-bq-sync --no-activate
gcloud config set account you@example.com --configuration zoho-bq-sync
gcloud config set project "$GCP_PROJECT" --configuration zoho-bq-sync
CLOUDSDK_CONFIG=$HOME/.config/gcloud-zoho-bq-sync gcloud auth application-default login
CLOUDSDK_CONFIG=$HOME/.config/gcloud-zoho-bq-sync gcloud auth application-default set-quota-project "$GCP_PROJECT"
```
Then uncomment `CLOUDSDK_ACTIVE_CONFIG_NAME` and `GOOGLE_APPLICATION_CREDENTIALS` in `.env` and run
`set -a; source .env; set +a` again. The global gcloud configuration and global ADC stay untouched.
Expected: `Quota project "<your project>" was added to ADC`, and `gcloud config get-value project`
prints `$GCP_PROJECT` in this shell only.

6. Validate the Zoho layout

```bash
python scripts/seed_demo_data.py check
```
Expected: `OK: Wedding_Bookings has all 13 fields and subform Booked_Services ...`.

7. Seed synthetic data

```bash
python scripts/seed_demo_data.py create --count 8
```
Expected: 8 lines `<id>  WB-SYN-000N ...  services=N`. Note one booking id as `DEMO_ID`.

8. Live integration tests: Zoho read-only, and BigQuery in a temporary dataset

```bash
RUN_LIVE_TESTS=1 BQ_TEST_PROJECT="$GCP_PROJECT" python -m pytest -m integration -q
```
Expected: `3 passed`. This proves the generated SQL runs on real BigQuery.

9. Rehearse the whole flow once (sections B.3-B.8), including the subform assumption: edit
   **only** a subform row of one booking (not `DEMO_ID`) in the Zoho UI, run
   `python -m zoho_bq_sync sync`, and check that the booking appears in the run. If it does not,
   keep a `sync --full` in the script. Make this edit **at least 5 minutes after seeding**. It
   then becomes the latest `Modified_Time`, so on demo day the seed batch sits outside the 300 s
   overlap window and the incremental run shows a small delta instead of re-reading all 8.
   Rehearse step 5 on that same non-demo booking, so `DEMO_ID` stays untouched for demo day.

10. Rehearsal creates `Apostille Deadline`. Delete it in Zoho afterwards, and use a fresh label on
   demo day if Zoho will not reuse the API name (then adjust `sql/demo/05_new_field_values.sql`).

11. Reset BigQuery so the demo starts with an automatic dataset and table creation

```bash
bq rm -r -f -d "$GCP_PROJECT:zoho_crm"
```
Expected: no output.

```bash
bq rm -r -f -d "$GCP_PROJECT:zoho_crm_staging"
```
Expected: no output.

12. Freeze the code so "no source changes" is provable

```bash
git init -q 2>/dev/null; git add -A && git commit -qm "demo baseline" && git status --short
```
Expected: empty output.

13. Open the tabs in advance: Zoho booking `DEMO_ID`, Zoho Setup -> Wedding Bookings layout,
    the BigQuery console, the terminal at the repo root, and the README diagram in the editor.

## B. Script (15:00)

### 0:00-2:00 Step 1: architecture
- README diagram: Scheduler -> Cloud Run Job -> Zoho v8 -> staging -> one transaction -> tables.
- Open `src/zoho_bq_sync/sync/engine.py` (`_sync_module`). Walk the pipeline: discover -> evolve -> extract -> transform -> deletions -> apply -> reconcile.
- Open `warehouse/sql.py` `transaction_script` and explain that the data and the checkpoint commit together.
- Open `sync/checkpoint.py` and explain the drift-safe checkpoint in one sentence.

### 2:00-3:30 Step 2: Zoho record with a subform
- Open booking `DEMO_ID`: parent fields plus 3-6 **Booked Services** rows. They are synthetic, as the `WB-SYN-` prefix and the example.com email show.

### 3:30-5:30 Step 3: run the pipeline (initial full load)

```bash
python -m zoho_bq_sync sync --summary
```
Expected: `bq_dataset_created` x2, `bq_table_created` for `_sync_state`, `_sync_runs`, `_reconciliation`, `wedding_bookings` and `wedding_bookings_services`, then `extraction_window mode=full reason=no checkpoint (initial load)`, `bq_transaction_committed`, `reconciliation ... status=OK` x2 and `module_sync_completed`.

Talking points: subform modules are listed as `id + Modified_Time`, then fetched per record,
because only `GET /{module}/{id}` returns subform rows. The data is loaded with load jobs, not
streaming.

### 5:30-7:00 Step 4: BigQuery parent and child

```bash
bq query --project_id="$GCP_PROJECT" --use_legacy_sql=false < sql/demo/01_parent_bookings.sql
```
Expected: 8 rows, typed columns (DATE, NUMERIC, TIMESTAMP in UTC).

```bash
bq query --project_id="$GCP_PROJECT" --use_legacy_sql=false < sql/demo/02_child_services.sql
```
Expected: all subform rows with `parent_id` and `_row_index`.

```bash
bq query --project_id="$GCP_PROJECT" --use_legacy_sql=false < sql/demo/03_parent_child_join.sql
```
Expected: per booking, `services_total` equals `package_price` (difference 0.00).

### 7:00-8:30 Step 5: change Zoho without touching code
1. In Zoho Setup, add the field **Apostille Deadline** (type Date) to Wedding Bookings and save the layout.
2. On booking `DEMO_ID`, set Apostille Deadline = **2027-05-20**, delete one Booked Services row, and save.
3. Show that the pipeline already sees the field (it only reads metadata):

```bash
python -m zoho_bq_sync discover --modules Wedding_Bookings --compare-bigquery
```
Expected: the row `apostille_deadline  DATE  Apostille_Deadline  date  NEW`.

### 8:30-10:00 Step 6: run again, with no code change

```bash
git status --short
```
Expected: empty output, so the source is unchanged.

```bash
python -m zoho_bq_sync sync --summary
```
Expected: `schema_evolved added=["apostille_deadline:DATE <- Apostille_Deadline"]`, then `extraction_window mode=incremental since=<checkpoint - 300s>` and `module_sync_completed new_columns=["wedding_bookings.apostille_deadline"]`.

### 10:00-11:30 Step 7: the new field in BigQuery

```bash
bq query --project_id="$GCP_PROJECT" --use_legacy_sql=false < sql/demo/04_schema_columns.sql
```
Expected: `apostille_deadline DATE YES` as the last column. No table was recreated and history is kept.

```bash
bq query --project_id="$GCP_PROJECT" --use_legacy_sql=false < sql/demo/05_new_field_values.sql
```
Expected: `DEMO_ID` has `2027-05-20`. The other rows are NULL, which is correct because they were never set in Zoho.

### 11:30-14:00 Step 8: incremental logs, checkpoints, reconciliation
- Point at the step 6 logs. `extraction_window` shows `checkpoint` and `since` = checkpoint - overlap. `extraction_completed` shows a small `listed` (`DEMO_ID` plus whatever the rehearsal edited within 300 s of the checkpoint), not 8. `checkpoint_committed` shows before and after. `zoho_deleted_records_fetched` shows the deletion poll.

```bash
bq query --project_id="$GCP_PROJECT" --use_legacy_sql=false < sql/demo/06_sync_state.sql
```
Expected: one row per module with the checkpoint equal to the max `Modified_Time` read, and `updated_at` = the last run.

```bash
bq query --project_id="$GCP_PROJECT" --use_legacy_sql=false < sql/demo/07_sync_runs.sql
```
Expected: two runs, `full` then `incremental`. The second has `new_columns=[wedding_bookings.apostille_deadline]` and far fewer `api_calls`.

```bash
bq query --project_id="$GCP_PROJECT" --use_legacy_sql=false < sql/demo/08_reconciliation.sql
```
Expected: `zoho_count_vs_bq_active` OK (8 = 8) and `child_rows_for_synced_parents` OK.

```bash
bq query --project_id="$GCP_PROJECT" --use_legacy_sql=false < sql/demo/09_deleted_rows.sql
```
Expected: the subform row deleted in step 5, with `_is_deleted = TRUE` and its `parent_id`.

Optional, if time allows: delete a whole booking in Zoho, run `sync` again, and query 09 shows
the booking plus its cascaded services.

### 14:00-15:00 Wrap-up
- `python -m pytest tests -q` runs 132 tests in under a second. They use mocks, and the demo says so.
- Deployment: `deploy/` (Cloud Run Job, secret mounted as a file, least-privilege SAs, Scheduler
  at 02:00, freshness job, log-based metrics). State plainly that it has not been deployed yet,
  if that is the case.
- Limits: 100k `page_token` cap means Bulk Read for large loads, detail fetch per changed record
  for subforms, retention of the deleted-records API.

## C. If something breaks live
- Zoho 429 or a timeout: the logs show `zoho_retry` with backoff, which itself demonstrates resilience. If credits are exhausted, switch to the rehearsal results and **say they come from the rehearsal run**.
- A BigQuery error: the module fails loudly (`module_sync_failed`) and both the table and the checkpoint stay untouched. Show query 06 to prove the checkpoint did not move, then re-run.
