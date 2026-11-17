-- Freshness: modules without a successful commit in the last 26 hours
SELECT module, updated_at AS last_success,
       TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), updated_at, MINUTE) / 60 AS hours_since_success,
       TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), updated_at, HOUR) > 26 AS is_stale
FROM zoho_crm._sync_state
ORDER BY hours_since_success DESC;
