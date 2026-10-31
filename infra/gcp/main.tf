# GCP side of Searchbox: the Gemini relay (this file) and the SIEM (siem.tf).
#
# Terraform creates the pieces that rarely change: APIs, a service account,
# and two empty secrets. The secret VALUES are added with gcloud (relay/README.md)
# so the Gemini key never lands in Terraform state. The relay itself is
# deployed with relay/deploy.sh, like the API.

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 8.0"
    }
  }

  # Same state storage as infra/azure, different key. Copy the three values
  # from the backend block in infra/azure (your versions.tf).
  backend "azurerm" {
    resource_group_name  = "CHANGE_ME"
    storage_account_name = "CHANGE_ME"
    container_name       = "CHANGE_ME"
    key                  = "gcp.tfstate"
    use_azuread_auth     = true
  }
}

variable "GCP_PROJECT" {
  type        = string
  description = "GCP project ID (Codespaces secret GCP_PROJECT → TF_VAR_GCP_PROJECT)"
}

variable "region" {
  type        = string
  default     = "asia-south1" # Mumbai: India is a Gemini API supported location
  description = "Region for the relay. Must be a country the Gemini API serves (not Hong Kong)."
}

provider "google" {
  project = var.GCP_PROJECT
  region  = var.region
}

locals {
  apis = [
    "run.googleapis.com",              # Cloud Run
    "cloudbuild.googleapis.com",       # builds the container for `gcloud run deploy --source`
    "artifactregistry.googleapis.com", # stores that container
    "secretmanager.googleapis.com",    # the Gemini key, relay token, SIEM API keys
    "iam.googleapis.com",              # service accounts and Vector's key
    "storage.googleapis.com",          # edge log bucket
    "bigquery.googleapis.com",         # SIEM dataset
    "cloudscheduler.googleapis.com",   # hourly SIEM job trigger
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# The relay runs as this account, which can read exactly two secrets and nothing else.
resource "google_service_account" "relay" {
  account_id   = "gemini-relay"
  display_name = "Searchbox Gemini relay"
}

resource "google_secret_manager_secret" "relay" {
  for_each  = toset(["gemini-api-key", "relay-token"])
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "relay_reads" {
  for_each  = google_secret_manager_secret.relay
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.relay.email}"
}

output "relay_service_account" {
  value = google_service_account.relay.email
}

output "relay_region" {
  value = var.region
}
