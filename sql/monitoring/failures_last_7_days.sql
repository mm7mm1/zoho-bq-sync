-- Failed module runs in the last 7 days
SELECT DATE(started_at) AS day, module, COUNT(*) AS failures, ANY_VALUE(error) AS sample_error
FROM zoho_crm._sync_runs
WHERE status = 'failed' AND started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY day, module
ORDER BY day DESC;
