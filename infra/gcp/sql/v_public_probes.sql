-- Exploit probes (paths matching the same regex Anubis denies), last 30 days.
-- Counted from Caddy's log, which sees every request on ports 80 and 443,
-- including direct hits on the VM's IP that never pass through Cloudflare.
SELECT
  path,
  COUNT(*)                   AS hits,
  COUNT(DISTINCT ip_network) AS networks,
  COUNT(DISTINCT country)    AS countries,
  MAX(ts)                    AS last_seen,
  APPROX_TOP_COUNT(user_agent, 1)[OFFSET(0)].value AS top_user_agent,
  COUNTIF(edge_path = 'direct') AS direct_hits
FROM `${project}.${dataset}.v_public_events`
WHERE event_source = 'caddy'
  AND is_probe
  AND ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY path
