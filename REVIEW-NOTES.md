# Review notes: Phases 3 + 4 (bot gate, telemetry, SIEM dashboard)

Unzip over `/workspaces/searchbox/` (check `git status` that nothing landed in a nested `searchbox/` folder). Delete this file after committing.

**Setup steps: [`siem/README.md`](siem/README.md)** (7 steps, then the checks table).

## New
| Path | What |
|---|---|
| `infra/azure/edge/phase3.sh` | installs Anubis 1.28.2 + Vector 0.61.0 on the edge (version + sha256 pinned), then the new Caddyfile |
| `infra/azure/edge/phase3/anubis-policy.yaml` | allow robots/.well-known, deny exploit probes, challenge everything else |
| `infra/azure/edge/phase3/probe-regex.txt` | the probe list, shared by Anubis (deny) and BigQuery (count) |
| `infra/azure/edge/phase3/vector.yaml` + `vector.tests.yaml` | log shipping with VRL transforms + 7 unit tests |
| `infra/gcp/siem.tf`, `infra/gcp/sql/*.sql` | bucket, dataset, 4 tables, 4 views, 3 service accounts, 2 secrets, scheduler |
| `siem/job/` | hourly Cloud Run job: load new log files, enrich new IPs (16 tests) |
| `siem/deploy.sh` | deploy the job + run it once |
| `siem/DASHBOARD.md` | Looker Studio spec: data sources, 8 panels, publish + embed |
| `api/SearchBox.Api/wwwroot/security.html`, `security.js` | `/security.html` frames the report (only a Looker embed URL is accepted) |

## Changed
| Path | Change |
|---|---|
| `infra/azure/edge/Caddyfile.tftpl` | pages → Anubis (with fallback page if Anubis or the app is down); `request_id` in the log; log file group-readable for Vector |
| `infra/gcp/main.tf` | enables IAM, Storage, BigQuery, Scheduler APIs |
| `api/SearchBox.Api/wwwroot/app.js`, `app.css` | "Security dashboard" link → `/security.html` |
| `api/deploy/api.env.example` | `SearchBox__DashboardUrl` explained |
| `crawler/antiseo_crawler/images.py`, `thumbs.py` | only `409 BlobAlreadyExists` counts as uploaded (the missing-images bug) |
| `README.md` | SIEM diagram + tier, layout, privacy bullet |

## How it was tested here
- **Anubis:** the real 1.28.2 binary with this policy: pages challenged, `/.env` `/wp-login.php` `/debug` denied, `robots.txt` served, metrics on localhost, no open-redirect warning.
- **Vector:** the real 0.61.0 binary. 7 unit tests use real Caddy and Anubis log lines. A file-to-file run produced the exact NDJSON BigQuery loads (query string dropped, junk line dropped). The full config validates.
- **SIEM job:** 16 tests with fake BigQuery/GCS/HTTP; the MERGE parameter is built with the real BigQuery client library.
- **View SQL:** rendered exactly as Terraform renders it and parsed with a BigQuery-dialect parser; Terraform files parsed as HCL.
- **Security page:** Chromium with the site's CSP; a valid embed URL is framed, a foreign URL is refused; no console errors.

**Not testable here, so check on deploy:**
- `terraform plan/apply` for GCP. Views are only fully validated by real BigQuery; if one fails, paste the error.
- `caddy validate` for the new Caddyfile. The deploy script validates before reloading, so a bad config never goes live.
- The GCS upload with a real key, and the first real load.

## Worth being able to explain
1. Why Anubis' default policy isn't enough (it waves curl through), and why `/api/*` skips it.
2. One regex enforced at the edge and counted in the warehouse.
3. The trust boundaries in the data: `cf_country` and `edge_path` only believed when Cloudflare is really in the path.
4. Privacy: no query strings, masked networks, your networks excluded, public views only.
5. Least privilege: create-only log writer, read-only job on the bucket, scheduler-only invoker, keys in headers.
6. Load-once semantics: a failed load marks nothing as loaded, so the next hour retries it.
7. Why the plan's Data Transfer Service became a job (fewer identities, testable, still free).
