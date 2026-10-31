-- Anubis outcomes per day: how many challenges were issued, solved, and how
-- many requests were denied outright. Built on the masked view.
SELECT
  day,
  COUNTIF(anubis_action = 'CHALLENGE') AS challenged,
  COUNTIF(anubis_action = 'PASS')      AS solved,
  COUNTIF(anubis_action = 'DENY')      AS denied,
  COUNT(DISTINCT IF(anubis_action = 'CHALLENGE', ip_network, NULL)) AS challenged_networks,
  COUNT(DISTINCT IF(anubis_action = 'PASS', ip_network, NULL))      AS solved_networks,
  SAFE_DIVIDE(COUNTIF(anubis_action = 'PASS'), COUNTIF(anubis_action = 'CHALLENGE')) AS solve_rate
FROM `${project}.${dataset}.v_public_events`
WHERE event_source = 'anubis'
GROUP BY day
