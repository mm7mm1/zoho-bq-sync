-- Step 8b: run history: mode, counts, schema changes, checkpoint movement, API usage
SELECT run_id, module, mode, mode_reason, status, records_extracted, parent_rows, child_rows,
       deleted_records, rows_inserted, rows_updated, new_columns, drift_detected,
       checkpoint_before, checkpoint_after, api_calls, error, started_at, finished_at
FROM zoho_crm._sync_runs
ORDER BY started_at DESC
LIMIT 20;
