# The only public machine: Caddy (TLS with the Cloudflare Origin CA cert),
# the fallback page and the /img/* proxy to Blob storage.
# Phase 3 adds Anubis and Vector here.

variable "TAILSCALE_AUTHKEY_EDGE" {
  type      = string
  sensitive = true # Codespace secret TF_VAR_TAILSCALE_AUTHKEY_EDGE (single-use, tagged tag:edge)
}

variable "ORIGIN_CERT_PEM" {
  type      = string
  sensitive = true # Codespace secret TF_VAR_ORIGIN_CERT_PEM: the Cloudflare Origin CA certificate
}

variable "ORIGIN_KEY_PEM" {
  type      = string
  sensitive = true # Codespace secret TF_VAR_ORIGIN_KEY_PEM: its private key
}

variable "site_host" {
  type    = string
  default = "search.theincorrect.one"
}

locals {
  caddyfile = templatefile("${path.module}/edge/Caddyfile.tftpl", {
    site_host = var.site_host
    app_ip    = azurerm_network_interface.appvmnic.private_ip_address
    blob_host = trimsuffix(trimprefix(azurerm_storage_account.thumbs.primary_blob_endpoint, "https://"), "/")
    read_sas  = trimprefix(data.azurerm_storage_account_blob_container_sas.edge_read.sas, "?")
  })
}

resource "azurerm_public_ip" "edge" {
  name                = "edge-pip"
  location            = azurerm_resource_group.resourcegrp.location
  resource_group_name = azurerm_resource_group.resourcegrp.name
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_network_interface" "edgevmnic" {
  name                = "edge-nic"
  location            = azurerm_resource_group.resourcegrp.location
  resource_group_name = azurerm_resource_group.resourcegrp.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.snetedge.id
    private_ip_address_allocation = "Static"
    private_ip_address            = "10.20.1.4" # the API trusts X-Forwarded-For only from this address
    public_ip_address_id          = azurerm_public_ip.edge.id
  }
}

resource "azurerm_linux_virtual_machine" "edgevm" {
  name                            = "edge-vm"
  resource_group_name             = azurerm_resource_group.resourcegrp.name
  location                        = azurerm_resource_group.resourcegrp.location
  size                            = "Standard_B2ats_v2" # x64, free 750 h/month
  admin_username                  = "azureuser"
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.edgevmnic.id]

  custom_data = base64encode(templatefile("${path.module}/cloud-init/edge.yaml", {
    ts_authkey  = var.TAILSCALE_AUTHKEY_EDGE
    origin_cert = var.ORIGIN_CERT_PEM
    origin_key  = var.ORIGIN_KEY_PEM
    caddyfile     = local.caddyfile
    fallback_html = file("${path.module}/edge/fallback.html")
  }))

  admin_ssh_key {
    username   = "azureuser"
    public_key = file("${path.module}/keys/antiseo.pub")
  }

  os_disk {
    name                 = "edge-vm-osdisk"
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = 64 # P6: free tier
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server" # x64 Gen2
    version   = "latest"
  }

  boot_diagnostics {}

  lifecycle {
    # cloud-init only runs on first boot. Later Caddyfile changes are pushed
    # with edge/deploy.sh, so they must not rebuild the VM.
    ignore_changes = [custom_data]
  }
}

output "edge_public_ip" {
  description = "Point the Cloudflare A record for search.theincorrect.one here (proxied)"
  value       = azurerm_public_ip.edge.ip_address
}

output "edge_caddyfile" {
  description = "Rendered Caddyfile (contains the read SAS). Used by edge/deploy.sh"
  value       = local.caddyfile
  sensitive   = true
}
