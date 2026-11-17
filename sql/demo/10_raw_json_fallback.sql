-- Any source attribute is still reachable through _raw_json, even if it is not mapped to a column
SELECT id, name,
       JSON_VALUE(_raw_json, '$.Owner.name') AS owner_name_from_raw,
       JSON_VALUE(_raw_json, '$.Modified_Time') AS modified_time_raw
FROM zoho_crm.wedding_bookings
LIMIT 10;
