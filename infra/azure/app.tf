variable "TAILSCALE_AUTHKEY_APP" {
  type      = string
  sensitive = true # from the TF_VAR_TAILSCALE_AUTHKEY_APP Codespace secret
}

resource "azurerm_network_interface" "appvmnic" {
  name                = "app-nic"
  location            = azurerm_resource_group.resourcegrp.location
  resource_group_name = azurerm_resource_group.resourcegrp.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.snetapp.id
    private_ip_address_allocation = "Static"
    private_ip_address            = "10.20.2.4"
  }
}

resource "azurerm_linux_virtual_machine" "appvm" {
  name                            = "app-vm"
  resource_group_name             = azurerm_resource_group.resourcegrp.name
  location                        = azurerm_resource_group.resourcegrp.location
  size                            = "Standard_B2pts_v2"
  admin_username                  = "azureuser"
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.appvmnic.id]

  custom_data = base64encode(templatefile("${path.module}/cloud-init/app.yaml", {
    ts_authkey = var.TAILSCALE_AUTHKEY_APP
  }))

  admin_ssh_key {
    username   = "azureuser"
    public_key = file("${path.module}/keys/antiseo.pub")
  }

  os_disk {
    name                 = "app-vm-osdisk"
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = 64
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server-arm64"
    version   = "latest"
  }

  boot_diagnostics {}

  lifecycle {
    ignore_changes = [custom_data] # rotating the Tailscale key must not rebuild the VM
  }
}

output "app_private_ip" {
  value = azurerm_network_interface.appvmnic.private_ip_address
}