# The SIEM (Phases 3-4): edge logs -> GCS -> BigQuery -> masked views -> Looker Studio.
#
#   edge VM: Vector ──(create-only key)──> gs://<project>-edge-logs/edge/*.ndjson.gz
#   hourly:  Cloud Scheduler ──> Cloud Run job "siem-job" (siem/job)
#              1. loads new files into siem.edge_events (load jobs: free)
#              2. looks up new IPs (IPinfo Lite, AbuseIPDB) into siem.ip_intel
#   views:   v_public_* mask IPs and drop your own networks; the dashboard
#            reads only those.
#
# All of it stays in GCP's free tier: GCS (us-central1, <5k writes/month),
# BigQuery (loads free, storage and queries far under the free amounts),
# one Cloud Run job, one scheduler job.

variable "siem_region" {
  type        = string
  default     = "us-central1" # GCS's Always Free tier only covers us-central1/us-east1/us-west1
  description = "Region for the log bucket, BigQuery dataset, SIEM job and scheduler"
}

locals {
  dataset     = "siem"
  probe_regex = trimspace(file("${path.module}/../azure/edge/phase3/probe-regex.txt"))
  view_vars = {
    project     = var.GCP_PROJECT
    dataset     = local.dataset
    probe_regex = local.probe_regex
  }
}

# ---------------- storage: raw logs ----------------

resource "google_storage_bucket" "edge_logs" {
  name                        = "${var.GCP_PROJECT}-edge-logs"
  location                    = upper(var.siem_region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # demo project: teardown takes the logs with it

  lifecycle_rule {
    condition { age = 90 }
    action { type = "Delete" }
  }
  depends_on = [google_project_service.apis]
}

# Vector on the edge VM writes with a key for this account. It can create
# objects in this one bucket: not read, list, overwrite or delete them.
resource "google_service_account" "vector" {
  account_id   = "vector-edge"
  display_name = "Vector on the Azure edge VM (writes logs only)"
}

resource "google_storage_bucket_iam_member" "vector_create" {
  bucket = google_storage_bucket.edge_logs.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.vector.email}"
}

# ---------------- BigQuery ----------------

resource "google_bigquery_dataset" "siem" {
  dataset_id                 = local.dataset
  location                   = upper(var.siem_region)
  description                = "Searchbox edge telemetry (Phase 3-4)"
  delete_contents_on_destroy = true
  depends_on                 = [google_project_service.apis]
}

resource "google_bigquery_table" "edge_events" {
  dataset_id          = google_bigquery_dataset.siem.dataset_id
  table_id            = "edge_events"
  description         = "One row per edge event (golden schema v3), loaded hourly from GCS"
  deletion_protection = false
  clustering          = ["event_source", "client_ip"]
  time_partitioning {
    type  = "DAY"
    field = "ts"
  }
  schema = jsonencode([
    { name = "ts", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "event_source", type = "STRING", description = "caddy | anubis" },
    { name = "edge_path", type = "STRING", description = "cloudflare | direct (caddy rows only)" },
    { name = "client_ip", type = "STRING" },
    { name = "cf_country", type = "STRING", description = "Cf-Ipcountry, only trusted via Cloudflare" },
    { name = "method", type = "STRING" },
    { name = "host", type = "STRING" },
    { name = "path", type = "STRING", description = "Without the query string" },
    { name = "status", type = "INT64" },
    { name = "latency_ms", type = "FLOAT64" },
    { name = "bytes_out", type = "INT64" },
    { name = "user_agent", type = "STRING" },
    { name = "route", type = "STRING", description = "api | img | web" },
    { name = "anubis_action", type = "STRING", description = "CHALLENGE | PASS | DENY (anubis rows only)" },
    { name = "anubis_rule", type = "STRING" },
    { name = "request_id", type = "STRING" },
  ])
}

resource "google_bigquery_table" "ip_intel" {
  dataset_id          = google_bigquery_dataset.siem.dataset_id
  table_id            = "ip_intel"
  description         = "What IPinfo Lite and AbuseIPDB say about each client IP (refreshed every 14 days)"
  deletion_protection = false
  schema = jsonencode([
    { name = "ip", type = "STRING", mode = "REQUIRED" },
    { name = "first_seen", type = "TIMESTAMP" },
    { name = "last_checked", type = "TIMESTAMP" },
    { name = "country", type = "STRING", description = "ISO 3166-1 alpha-2" },
    { name = "asn", type = "STRING" },
    { name = "as_name", type = "STRING" },
    { name = "as_domain", type = "STRING" },
    { name = "abuse_score", type = "INT64", description = "AbuseIPDB confidence, 0-100" },
    { name = "total_reports", type = "INT64" },
    { name = "usage_type", type = "STRING" },
    { name = "isp", type = "STRING" },
    { name = "is_tor", type = "BOOL" },
  ])
}

resource "google_bigquery_table" "loaded_files" {
  dataset_id          = google_bigquery_dataset.siem.dataset_id
  table_id            = "loaded_files"
  description         = "GCS objects already loaded into edge_events (so each loads once)"
  deletion_protection = false
  schema = jsonencode([
    { name = "name", type = "STRING", mode = "REQUIRED" },
    { name = "loaded_at", type = "TIMESTAMP" },
  ])
}

resource "google_bigquery_table" "excluded_networks" {
  dataset_id          = google_bigquery_dataset.siem.dataset_id
  table_id            = "excluded_networks"
  description         = "Your own networks (CIDR or single IP), hidden from the public views"
  deletion_protection = false
  schema = jsonencode([
    { name = "cidr", type = "STRING", mode = "REQUIRED", description = "e.g. 49.36.0.0/16 or 2401:4900::/32" },
    { name = "note", type = "STRING" },
  ])
}

resource "google_bigquery_table" "v_events_enriched" {
  dataset_id          = google_bigquery_dataset.siem.dataset_id
  table_id            = "v_events_enriched"
  description         = "Private: events + IP intel, full IPs"
  deletion_protection = false
  view {
    query          = templatefile("${path.module}/sql/v_events_enriched.sql", local.view_vars)
    use_legacy_sql = false
  }
  depends_on = [google_bigquery_table.edge_events, google_bigquery_table.ip_intel]
}

resource "google_bigquery_table" "v_public_events" {
  dataset_id          = google_bigquery_dataset.siem.dataset_id
  table_id            = "v_public_events"
  description         = "Public: masked IPs, your networks excluded. The dashboard's only row-level source."
  deletion_protection = false
  view {
    query          = templatefile("${path.module}/sql/v_public_events.sql", local.view_vars)
    use_legacy_sql = false
  }
  depends_on = [google_bigquery_table.v_events_enriched, google_bigquery_table.excluded_networks]
}

resource "google_bigquery_table" "v_public_funnel" {
  dataset_id          = google_bigquery_dataset.siem.dataset_id
  table_id            = "v_public_funnel"
  description         = "Public: Anubis challenged / solved / denied per day"
  deletion_protection = false
  view {
    query          = templatefile("${path.module}/sql/v_public_funnel.sql", local.view_vars)
    use_legacy_sql = false
  }
  depends_on = [google_bigquery_table.v_public_events]
}

resource "google_bigquery_table" "v_public_probes" {
  dataset_id          = google_bigquery_dataset.siem.dataset_id
  table_id            = "v_public_probes"
  description         = "Public: top exploit-probe paths, last 30 days"
  deletion_protection = false
  view {
    query          = templatefile("${path.module}/sql/v_public_probes.sql", local.view_vars)
    use_legacy_sql = false
  }
  depends_on = [google_bigquery_table.v_public_events]
}

# ---------------- the hourly job ----------------

# Runs siem/job (deployed with siem/deploy.sh). Reads the bucket, writes the dataset.
resource "google_service_account" "siem_job" {
  account_id   = "siem-job"
  display_name = "SIEM loader + IP enrichment job"
}

resource "google_project_iam_member" "siem_job_bq_jobs" {
  project = var.GCP_PROJECT
  role    = "roles/bigquery.jobUser" # run load and query jobs (project-level by design)
  member  = "serviceAccount:${google_service_account.siem_job.email}"
}

resource "google_bigquery_dataset_iam_member" "siem_job_data" {
  dataset_id = google_bigquery_dataset.siem.dataset_id
  role       = "roles/bigquery.dataEditor" # this dataset only
  member     = "serviceAccount:${google_service_account.siem_job.email}"
}

resource "google_storage_bucket_iam_member" "siem_job_read" {
  bucket = google_storage_bucket.edge_logs.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.siem_job.email}"
}

# Free API keys for enrichment. Values are added with gcloud (siem/README.md),
# not Terraform, so they stay out of state. Either can be left empty.
resource "google_secret_manager_secret" "siem" {
  for_each  = toset(["abuseipdb-key", "ipinfo-token"])
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "siem_job_reads" {
  for_each  = google_secret_manager_secret.siem
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.siem_job.email}"
}

# The scheduler calls the Cloud Run Admin API as this account. siem/deploy.sh
# grants it run.invoker on the job itself (the job doesn't exist until then).
resource "google_service_account" "siem_scheduler" {
  account_id   = "siem-scheduler"
  display_name = "Triggers the SIEM job hourly"
}

resource "google_cloud_scheduler_job" "siem_hourly" {
  name             = "siem-hourly"
  region           = var.siem_region
  description      = "Load new edge logs and enrich new IPs"
  schedule         = "7 * * * *" # minute 7 of every hour, UTC
  time_zone        = "Etc/UTC"
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.GCP_PROJECT}/locations/${var.siem_region}/jobs/siem-job:run"
    oauth_token {
      service_account_email = google_service_account.siem_scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_project_service.apis]
}

# ---------------- outputs ----------------

output "edge_logs_bucket" {
  value = google_storage_bucket.edge_logs.name
}

output "vector_service_account" {
  value = google_service_account.vector.email
}

output "siem_job_service_account" {
  value = google_service_account.siem_job.email
}

output "siem_scheduler_service_account" {
  value = google_service_account.siem_scheduler.email
}

output "siem_region" {
  value = var.siem_region
}
