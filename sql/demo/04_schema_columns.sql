-- Step 7a: the column for the new Zoho field exists (added by the pipeline, no code change)
SELECT column_name, data_type, is_nullable, ordinal_position
FROM zoho_crm.INFORMATION_SCHEMA.COLUMNS
WHERE table_name = 'wedding_bookings'
ORDER BY ordinal_position;
