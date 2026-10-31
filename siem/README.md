# SIEM: bot gate + telemetry (Phase 3) and dashboard (Phase 4)

Every request that reaches the edge, human or bot, ends up in BigQuery and on a public dashboard.

```
            Cloudflare / direct scanners
                       │
  edge VM   Caddy ── pages ──> Anubis (proof-of-work) ──> app VM
              │                  │
     access.json            journald
              └──────┬───────────┘
                  Vector  (VRL: one row per event, query strings dropped)
                     │  every 15 min, gzip NDJSON, create-only key
  GCP (us-central1)  ▼
            gs://<project>-edge-logs/edge/*.ndjson.gz   (deleted after 90 days)
                     │  hourly: Cloud Scheduler → Cloud Run job "siem-job"
                     ▼     1. load new files (free load jobs, each file once)
            BigQuery siem.edge_events ── 2. enrich new IPs ──> siem.ip_intel
                     │                     (IPinfo Lite, AbuseIPDB)
                     ▼
            v_public_events / v_public_funnel / v_public_probes
            (IPs masked to /24 and /48, your networks excluded)
                     │
            Looker Studio report ──> embedded at /security.html
```

## What's where
| Path | What |
|---|---|
| `infra/azure/edge/phase3.sh` | installs Anubis + Vector on the edge (pinned versions, sha256-checked), then switches Caddy |
| `infra/azure/edge/phase3/anubis-policy.yaml` | allow `robots.txt`/`.well-known`, **deny** exploit probes, **challenge** everything else |
| `infra/azure/edge/phase3/probe-regex.txt` | the probe list, used by both Anubis (deny) and BigQuery (count) |
| `infra/azure/edge/phase3/vector.yaml` | sources, VRL transforms, GCS sink; `vector.tests.yaml` = 7 unit tests |
| `infra/gcp/siem.tf` | bucket, dataset, tables, views, service accounts, secrets, scheduler |
| `infra/gcp/sql/*.sql` | the four views |
| `siem/job/` | the hourly job (load + enrich); `test_job.py` = 16 tests |
| `siem/deploy.sh` | deploys the job and runs it once |
| `siem/DASHBOARD.md` | building the Looker Studio report |

## Design notes (worth knowing for interviews)
- **Anubis challenges everyone.** Its default policy lets non-browser clients (curl, Go-http-client, most scanners) straight through; this one doesn't, so the funnel shows real bot behaviour. `/api/*` and `/img/*` skip Anubis (app clients can't solve proof-of-work) and keep their own rate limits.
- **One regex, two enforcement points.** `probe-regex.txt` is RE2 syntax, which both Go (Anubis) and BigQuery use, so what's denied and what's counted can't drift apart.
- **Trust boundaries in the data.** `cf_country` is kept only when the request really came through Cloudflare, since a direct visitor can send any `Cf-Ipcountry` header. `edge_path` is derived from Caddy's trusted-proxy check (client IP differs from socket IP only via Cloudflare).
- **Privacy.** Query strings (search terms) never leave the edge. The public views mask IPs to networks, exclude your own networks, and are the dashboard's only source.
- **Least privilege.** Vector's key can *create* objects in one bucket: no read, list, overwrite or delete. The job can read that bucket and edit one dataset. Only the scheduler's account can start the job. API keys sit in Secret Manager, sent in headers, never in URLs.
- **Changed from the plan:** the plan used BigQuery Data Transfer Service for GCS→BigQuery. The job loads files itself instead (a load job per hour, a `loaded_files` table so each file loads once). That means one scheduler and one service account instead of a transfer config plus its own identity, and loads that are testable in code. It's equally free.
- **Free tier:** GCS ~2.9k writes/month (< 5k), BigQuery loads free and data in MBs, one Cloud Run job (~30 s/hour), one scheduler job (3 free), AbuseIPDB ≤ 960 checks/day (1,000 free).

---

## Setup

### 1. Free API keys (optional, recommended)
- **IPinfo:** sign up at ipinfo.io → copy the token. Lite = country + ASN, unlimited.
- **AbuseIPDB:** sign up at abuseipdb.com → Account → API → create a key (1,000 checks/day free).

Either can be skipped; the dashboard then has less detail.

### 2. Terraform (Codespace)
```bash
cd /workspaces/searchbox/infra/gcp
terraform init -upgrade
terraform plan -out siem.tfplan    # expect ~20 to add: bucket, dataset, 4 tables, 4 views, 3 service accounts, IAM, 2 secrets, scheduler
terraform apply siem.tfplan
```
If a view fails to create, BigQuery prints the exact SQL error. Paste it, because views can only be checked against real BigQuery.

### 3. Put the keys in Secret Manager
```bash
read -rsp "IPinfo token: " K; echo; printf %s "$K" | gcloud secrets versions add ipinfo-token --data-file=-; unset K
read -rsp "AbuseIPDB key: " K; echo; printf %s "$K" | gcloud secrets versions add abuseipdb-key --data-file=-; unset K
```

### 4. Hide your own networks from the public views
Find your public IP (`curl -s https://api.ipify.org`) and add its network. Mobile and home IPs in India change often, so a /16 of your ISP's range is more practical than a single address:
```bash
bq query --use_legacy_sql=false \
  "INSERT siem.excluded_networks (cidr, note) VALUES ('49.36.0.0/16', 'home ISP'), ('2401:4900::/32', 'mobile v6')"
```

### 5. Edge: Anubis + Vector (Codespace)
```bash
cd /workspaces/searchbox/infra/azure && terraform apply      # updates the edge_caddyfile output (no VM change)
cd /workspaces/searchbox && infra/azure/edge/phase3.sh
```
The script:
1. creates Vector's key, unless the edge already has one;
2. installs and starts Anubis, and checks that it serves a challenge;
3. installs and starts Vector;
4. deploys the new Caddyfile; Caddy validates it before reloading.

Then:
```bash
curl -s https://search.theincorrect.one/ | grep -o '<title>[^<]*'     # Anubis' "not a bot" page
curl -s https://search.theincorrect.one/api/v1/health                  # JSON, no challenge
```
Open the site in a browser: a short challenge, then search as usual.

### 6. The hourly job (Codespace)
```bash
siem/deploy.sh
```
It deploys the job and runs it once. The first run only finds files once Vector has written one (~15 min after step 5).

### 7. Dashboard
Follow [`DASHBOARD.md`](DASHBOARD.md), then on the app VM add the embed URL and restart:
```bash
sudo nano /etc/searchbox/api.env     # SearchBox__DashboardUrl=https://lookerstudio.google.com/embed/reporting/<id>/page/<page>
sudo systemctl restart searchbox-api
```
The site header gets a **Security dashboard** link to `/security.html`, which frames the report.

---

## Checks (Phase 3-4 exit tests)
| Check | How | Expected |
|---|---|---|
| Challenge | Private window → the site | proof-of-work page, then search |
| Probe denied | `curl -s https://search.theincorrect.one/.env \| grep -o '<title>[^<]*'` | "Oh noes!" (Anubis deny page) |
| API not gated | `curl -s https://search.theincorrect.one/api/v1/health` | JSON |
| Logs flowing | `gcloud storage ls gs://<project>-edge-logs/edge/` | a new file every ~15 min |
| Loaded | `bq query --use_legacy_sql=false 'SELECT event_source, COUNT(*) FROM siem.edge_events GROUP BY 1'` | caddy and anubis rows within ~75 min |
| Denied probes visible | `bq query --use_legacy_sql=false 'SELECT * FROM siem.v_public_probes ORDER BY hits DESC LIMIT 10'` | `/.env` and friends |
| Masking | `bq query --use_legacy_sql=false 'SELECT ip_network FROM siem.v_public_events LIMIT 5'` | `x.y.z.0/24` style only |
| Enrichment | `bq query --use_legacy_sql=false 'SELECT country, as_name, abuse_band, COUNT(*) FROM siem.v_public_events GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 10'` | countries and networks filled in |
| Vector is create-only | `gcloud storage buckets get-iam-policy gs://<project>-edge-logs` | `vector-edge` has only `roles/storage.objectCreator` |
| Dashboard | `/security.html` in an incognito window | the report loads |

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| Site shows the "search node is offline" page right after phase3.sh | Anubis isn't reachable: `ssh antiseo-edge sudo journalctl -u anubis@searchbox -n 30` |
| Challenge loops (solves, then challenges again) | cookie bound to a changing IP (Anubis binds the pass to X-Real-Ip); normal on some mobile networks |
| No files in the bucket after 20 min | `ssh antiseo-edge sudo journalctl -u vector -n 50`: a key or bucket-name problem shows there |
| Vector: `Permission denied` on access.json | `sudo chgrp caddy /var/log/caddy/access*.json && sudo chmod 640 ...`, then restart Vector |
| Job log: `{"step":"load","error":...}` | load failed; no file is marked as loaded, so the next run retries |
| `rate_limited: ["abuseipdb"]` | daily quota hit; the rest is enriched tomorrow |
| A view fails in `terraform apply` | paste the error; views are only checkable against real BigQuery |
