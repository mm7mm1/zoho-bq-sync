-- Soft-deleted rows: deleted records, cascaded children and removed subform rows
SELECT 'wedding_bookings' AS table_name, id, CAST(NULL AS STRING) AS parent_id, _deleted_at, _run_id
FROM zoho_crm.wedding_bookings
WHERE _is_deleted
UNION ALL
SELECT 'wedding_bookings_services', id, parent_id, _deleted_at, _run_id
FROM zoho_crm.wedding_bookings_services
WHERE _is_deleted
ORDER BY _deleted_at DESC;
