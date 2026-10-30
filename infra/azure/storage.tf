# Thumbnail storage: one private container, reached only with SAS tokens.
#
#   crawler (Pi)  --write-only SAS-->  thumbs container  <--read-only SAS--  Caddy /img/*
#
# Nothing is publicly listable or readable without going through Caddy, and
# Cloudflare caches what Caddy serves (free tier: 20K reads, 10K writes/month).

resource "random_string" "sa_suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_storage_account" "thumbs" {
  name                     = "antiseothumbs${random_string.sa_suffix.result}" # globally unique, 3-24 lowercase
  resource_group_name      = azurerm_resource_group.resourcegrp.name
  location                 = azurerm_resource_group.resourcegrp.location
  account_tier             = "Standard"
  account_replication_type = "LRS" # free tier covers LRS hot
  account_kind             = "StorageV2"
  access_tier              = "Hot"

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false # no anonymous access, ever
  shared_access_key_enabled       = true  # needed to sign the SAS tokens below
}

resource "azurerm_storage_container" "thumbs" {
  name                  = "thumbs"
  storage_account_id    = azurerm_storage_account.thumbs.id
  container_access_type = "private"
}

# Fixed dates, so the tokens don't change on every plan.
# To revoke both tokens early: rotate the account key
#   az storage account keys renew -g rg-antiseo -n <account> --key key1
locals {
  sas_start  = "2026-10-01" # date only: the provider rejects UTC offsets
  sas_expiry = "2027-10-31"
}

# Crawler: may create/write blobs. Cannot read, list or delete.
data "azurerm_storage_account_blob_container_sas" "crawler_write" {
  connection_string = azurerm_storage_account.thumbs.primary_connection_string
  container_name    = azurerm_storage_container.thumbs.name
  https_only        = true
  start             = local.sas_start
  expiry            = local.sas_expiry

  permissions {
    read   = false
    add    = false
    create = true
    write  = true
    delete = false
    list   = false
  }
}

# Caddy on the edge: may read blobs. Cannot list, write or delete.
data "azurerm_storage_account_blob_container_sas" "edge_read" {
  connection_string = azurerm_storage_account.thumbs.primary_connection_string
  container_name    = azurerm_storage_container.thumbs.name
  https_only        = true
  start             = local.sas_start
  expiry            = local.sas_expiry

  permissions {
    read   = true
    add    = false
    create = false
    write  = false
    delete = false
    list   = false
  }
}

output "blob_base_url" {
  description = "BLOB_BASE_URL for ~/.antiseo/crawler.env on the Pi"
  value       = "${azurerm_storage_account.thumbs.primary_blob_endpoint}${azurerm_storage_container.thumbs.name}"
}

output "crawler_blob_sas" {
  description = "BLOB_SAS for ~/.antiseo/crawler.env on the Pi (terraform output -raw crawler_blob_sas)"
  value       = trimprefix(data.azurerm_storage_account_blob_container_sas.crawler_write.sas, "?")
  sensitive   = true
}
