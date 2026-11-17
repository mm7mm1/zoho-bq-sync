-- Step 8a: persistent checkpoints (advanced in the same transaction as the data)
SELECT module, table_name, last_modified_time AS checkpoint, last_deleted_check,
       last_full_sync_at, last_run_id, updated_at AS last_success
FROM zoho_crm._sync_state
ORDER BY module;
