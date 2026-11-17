-- Step 7b: the new field's value arrived; untouched historical rows stay NULL
SELECT id, name, apostille_deadline, modified_time, _run_id
FROM zoho_crm.wedding_bookings
WHERE _is_deleted IS NOT TRUE
ORDER BY apostille_deadline IS NULL, modified_time DESC;
