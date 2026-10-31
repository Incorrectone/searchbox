-- Every edge event plus what the enrichment job learned about its IP.
-- Private: contains full IPs. Only the SIEM job and you query this.
SELECT
  e.*,
  i.country       AS ip_country,
  i.asn,
  i.as_name,
  i.abuse_score,
  i.total_reports,
  i.usage_type,
  i.isp,
  i.is_tor,
  CASE
    WHEN i.abuse_score IS NULL THEN 'unknown'
    WHEN i.abuse_score < 25    THEN 'benign'
    WHEN i.abuse_score <= 75   THEN 'suspicious'
    ELSE 'malicious'
  END AS abuse_band
FROM `${project}.${dataset}.edge_events` AS e
LEFT JOIN `${project}.${dataset}.ip_intel` AS i
  ON i.ip = e.client_ip
