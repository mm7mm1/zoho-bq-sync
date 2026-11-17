-- Step 4b: subform rows in their own table; parent_id is the Zoho id of the booking
SELECT
  parent_id,
  _row_index,
  id AS subform_row_id,
  service_name,
  service_category,
  vendor_name,
  service_date,
  quantity,
  unit_price
FROM zoho_crm.wedding_bookings_services
WHERE _is_deleted IS NOT TRUE
ORDER BY parent_id, _row_index;
