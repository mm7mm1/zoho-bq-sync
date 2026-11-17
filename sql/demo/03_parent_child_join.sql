-- Step 4c: parent-child relationship preserved: package price vs sum of booked services
SELECT
  b.id,
  b.name,
  b.package_price,
  COUNT(s.id) AS services,
  SUM(s.unit_price * s.quantity) AS services_total,
  b.package_price - SUM(s.unit_price * s.quantity) AS difference
FROM zoho_crm.wedding_bookings AS b
LEFT JOIN zoho_crm.wedding_bookings_services AS s
  ON s.parent_id = b.id AND s._is_deleted IS NOT TRUE
WHERE b._is_deleted IS NOT TRUE
GROUP BY b.id, b.name, b.package_price
ORDER BY b.name;
