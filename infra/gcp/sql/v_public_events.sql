-- The ONLY source for the public dashboard.
--   * IPs masked: IPv4 to /24, IPv6 to /48 (a network, never a person).
--   * Rows from networks listed in excluded_networks (your own) are left out.
--   * No query strings anywhere (Vector already drops them).
WITH excluded AS (
  SELECT
    NET.SAFE_IP_FROM_STRING(SPLIT(cidr, '/')[OFFSET(0)]) AS net,
    SAFE_CAST(SPLIT(cidr, '/')[SAFE_OFFSET(1)] AS INT64) AS len
  FROM `${project}.${dataset}.excluded_networks`
),
events AS (
  SELECT *, NET.SAFE_IP_FROM_STRING(client_ip) AS ip_bytes
  FROM `${project}.${dataset}.v_events_enriched`
)
SELECT
  ts,
  DATE(ts) AS day,
  event_source,
  edge_path,
  CASE
    WHEN ip_bytes IS NULL THEN NULL
    WHEN BYTE_LENGTH(ip_bytes) = 4 THEN CONCAT(NET.IP_TO_STRING(NET.IP_TRUNC(ip_bytes, 24)), '/24')
    ELSE CONCAT(NET.IP_TO_STRING(NET.IP_TRUNC(ip_bytes, 48)), '/48')
  END AS ip_network,
  COALESCE(cf_country, ip_country) AS country,
  asn,
  as_name,
  usage_type,
  is_tor,
  abuse_score,
  abuse_band,
  method,
  path,
  status,
  route,
  anubis_action,
  anubis_rule,
  user_agent,
  latency_ms,
  REGEXP_CONTAINS(path, r'${probe_regex}') AS is_probe
FROM events
WHERE NOT EXISTS (
  SELECT 1
  FROM excluded AS x
  WHERE x.net IS NOT NULL
    AND events.ip_bytes IS NOT NULL
    AND BYTE_LENGTH(x.net) = BYTE_LENGTH(events.ip_bytes)
    AND NET.IP_TRUNC(events.ip_bytes, COALESCE(x.len, BYTE_LENGTH(x.net) * 8))
      = NET.IP_TRUNC(x.net, COALESCE(x.len, BYTE_LENGTH(x.net) * 8))
)
