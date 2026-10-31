# Building the public dashboard (Looker Studio)

Looker Studio reports are built in the browser, not in code. This is the spec: about 30–45 minutes the first time.
Use only the **`v_public_*`** views. They're the ones with masked IPs and your networks removed.

## 1. Data sources
lookerstudio.google.com → **Create → Data source → BigQuery** → your project → dataset `siem`. Add three:

| Data source | Table | Settings |
|---|---|---|
| **Events** | `v_public_events` | Date range dimension: `ts` |
| **Funnel** | `v_public_funnel` | Date range dimension: `day` |
| **Probes** | `v_public_probes` | (already the last 30 days) |

For each one: **Data credentials → Owner's credentials**, so visitors don't need Google Cloud access, and **Data freshness → 15 minutes**.

In **Events**, set field types:
- `country` → *Geo → Country*;
- `ts` → *Date & Time*.

Then add these calculated fields:
| Name | Formula |
|---|---|
| `Requests` | `SUM(CASE WHEN event_source = "caddy" THEN 1 ELSE 0 END)` |
| `Challenged` | `SUM(CASE WHEN anubis_action = "CHALLENGE" THEN 1 ELSE 0 END)` |
| `Denied` | `SUM(CASE WHEN anubis_action = "DENY" THEN 1 ELSE 0 END)` |
| `Probes` | `SUM(CASE WHEN event_source = "caddy" AND is_probe THEN 1 ELSE 0 END)` |
| `Denied %` | `Denied / Challenged` (format: percent) |
| `Networks` | `COUNT_DISTINCT(ip_network)` |

Why only `caddy` rows for `Requests`: Caddy logs every request once, while Anubis only logs its decisions. Counting both would double-count.

## 2. Report layout (one page, ~1200 px wide)
Title: **"SearchBox: live edge telemetry"**. Subtitle: *"Near-real-time (≤ 1 h). IPs masked to /24 (IPv4) and /48 (IPv6)."*

Add a **Date range control** at the top, defaulting to *Last 7 days*.

| # | Chart | Source | Dimension | Metrics | Notes |
|---|---|---|---|---|---|
| 1 | 4 × **Scorecard** | Events | – | `Requests`, `Networks`, `Probes`, `Denied` | compare to previous period |
| 2 | **Geo chart** | Events | `country` | `Requests` | filter `event_source = caddy` |
| 3 | **Time series** | Funnel | `day` | `challenged`, `solved`, `denied` | the Anubis funnel |
| 4 | **Table** "Top networks" | Events | `as_name`, `asn`, `abuse_band` | `Requests`, `Networks` | sort by Requests, 15 rows |
| 5 | **Table** "Exploit probes (30 days)" | Probes | `path`, `top_user_agent` | `hits`, `networks`, `countries`, `direct_hits` | sort by hits |
| 6 | **Donut** "How they arrive" | Events | `edge_path` | `Requests` | `cloudflare` vs `direct` hits on the VM's IP |
| 7 | **Bar** "Abuse score bands" | Events | `abuse_band` | `Networks` | benign / suspicious / malicious / unknown |
| 8 | **Bar** "What they ask for" | Events | `route` | `Requests` | web / api / img |

Panel 6 is usually the surprise in an interview: direct scans of the Azure IP that never go through Cloudflare.

## 3. Publish and embed
1. **Share → Manage access → Anyone on the internet can view.**
2. **File → Embed report → Enable embedding → Embed URL.** Copy it; it looks like `https://lookerstudio.google.com/embed/reporting/<id>/page/<page>`.
3. On the app VM:
   ```bash
   sudo nano /etc/searchbox/api.env      # SearchBox__DashboardUrl=<that embed URL>
   sudo systemctl restart searchbox-api
   ```
4. Open `https://search.theincorrect.one/security.html` in an **incognito** window. It should load without a Google login.

The page only frames URLs of exactly that `lookerstudio.google.com/embed/reporting/...` shape; anything else shows "not published yet".

## Cost guard
Every chart on a page view runs a query **with your credentials**. They stay cheap because:
- `edge_events` is partitioned by day, so the date control limits what's scanned;
- the data is MBs per day, against BigQuery's free 1 TB/month of queries;
- the 15-minute freshness setting means Looker caches results instead of re-querying on every view.
