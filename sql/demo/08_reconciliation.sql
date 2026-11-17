-- Step 8c: row-count reconciliation of the latest runs
SELECT run_id, module, table_name, check_name, expected, actual, difference, status, details, checked_at
FROM zoho_crm._reconciliation
ORDER BY checked_at DESC
LIMIT 20;
