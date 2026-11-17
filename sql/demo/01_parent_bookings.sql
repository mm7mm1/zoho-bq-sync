-- Step 4a: parent table, one row per Zoho Wedding_Bookings record
SELECT
  id,
  name,
  ceremony_type,
  destination_country,
  ceremony_city,
  wedding_date,
  guest_count,
  package_price,
  booking_status,
  modified_time,
  _synced_at,
  _run_id
FROM zoho_crm.wedding_bookings
WHERE _is_deleted IS NOT TRUE
ORDER BY modified_time DESC;
