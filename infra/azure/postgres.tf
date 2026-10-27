# Private DNS so the VNet can resolve the server's name to its private IP
resource "azurerm_private_dns_zone" "pg" {
  name                = "antiseo.private.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.resourcegrp.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "pg" {
  name                  = "pg-vnet-link"
  resource_group_name   = azurerm_resource_group.resourcegrp.name
  private_dns_zone_name = azurerm_private_dns_zone.pg.name
  virtual_network_id    = azurerm_virtual_network.subnetvn.id
  registration_enabled  = false
}

resource "random_password" "pg_admin" {
  length  = 32
  special = false # avoids escaping problems in connection strings
}

resource "random_string" "pg_suffix" {
  length  = 5
  upper   = false
  special = false # server names are globally unique
}

resource "azurerm_postgresql_flexible_server" "pg" {
  name                = "pg-antiseo-${random_string.pg_suffix.result}"
  resource_group_name = azurerm_resource_group.resourcegrp.name
  location            = azurerm_resource_group.resourcegrp.location

  version    = "17"
  sku_name   = "B_Standard_B1ms" # free 750 h/month
  storage_mb = 32768             # free 32 GB

  backup_retention_days        = 7
  geo_redundant_backup_enabled = false
  auto_grow_enabled            = false

  delegated_subnet_id           = azurerm_subnet.snetdb.id
  private_dns_zone_id           = azurerm_private_dns_zone.pg.id
  public_network_access_enabled = false

  administrator_login    = "pgadmin"
  administrator_password = random_password.pg_admin.result

  depends_on = [azurerm_private_dns_zone_virtual_network_link.pg]

  lifecycle {
    ignore_changes = [zone] # Azure picks a zone; don't fight it on every plan
  }
}

# Allowlist pgvector (CREATE EXTENSION still has to be run inside the DB)
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.pg.id
  value     = "VECTOR"
}

output "pg_fqdn" {
  value = azurerm_postgresql_flexible_server.pg.fqdn
}

output "pg_admin_password" {
  value     = random_password.pg_admin.result
  sensitive = true
}