resource "azurerm_resource_group" "resourcegrp" {
  name     = "rg-antiseo"
  location = var.location
}

resource "azurerm_virtual_network" "subnetvn" {
  name                = "vn-antiseo"
  resource_group_name = azurerm_resource_group.resourcegrp.name
  location            = azurerm_resource_group.resourcegrp.location
  address_space       = ["10.20.0.0/16"]
}

resource "azurerm_subnet" "snetapp" {
  name                 = "snet-app"
  resource_group_name  = azurerm_resource_group.resourcegrp.name
  virtual_network_name = azurerm_virtual_network.subnetvn.name
  address_prefixes     = ["10.20.2.0/24"]

  default_outbound_access_enabled = true
}

resource "azurerm_subnet" "snetdb" {
  name                 = "snet-db"
  resource_group_name  = azurerm_resource_group.resourcegrp.name
  virtual_network_name = azurerm_virtual_network.subnetvn.name
  address_prefixes     = ["10.20.3.0/24"]

  delegation {
    name = "pg"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "snetpe" {
  name                 = "snet-pe"
  resource_group_name  = azurerm_resource_group.resourcegrp.name
  virtual_network_name = azurerm_virtual_network.subnetvn.name
  address_prefixes     = ["10.20.4.0/24"]

  private_endpoint_network_policies = "Enabled"
}

resource "azurerm_subnet" "snetedge" {
  name                 = "snet-edge"
  resource_group_name  = azurerm_resource_group.resourcegrp.name
  virtual_network_name = azurerm_virtual_network.subnetvn.name
  address_prefixes     = ["10.20.1.0/24"]
}

resource "azurerm_network_security_group" "snetappsg" {
  name                = "snetapp-sg"
  location            = azurerm_resource_group.resourcegrp.location
  resource_group_name = azurerm_resource_group.resourcegrp.name

  security_rule {
    name                       = "allowtcp"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8080"
    source_address_prefix      = "10.20.1.0/24"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "alldeny"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "appsubnetsg" {
  subnet_id                 = azurerm_subnet.snetapp.id
  network_security_group_id = azurerm_network_security_group.snetappsg.id
}

resource "azurerm_network_security_group" "snetedgesg" {
  name                = "snetedge-sg"
  location            = azurerm_resource_group.resourcegrp.location
  resource_group_name = azurerm_resource_group.resourcegrp.name

  security_rule {
    name                       = "allowtcp"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allowtcpe"
    priority                   = 105
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allowtcpout"
    priority                   = 100
    direction                  = "Outbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8080"
    source_address_prefix      = "*"
    destination_address_prefix = "10.20.2.0/24"
  }

  security_rule {
    name                       = "allowudp"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Udp"
    source_port_range          = "*"
    destination_port_range     = "41641"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "alldeny"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "alldenyout"
    priority                   = 4000
    direction                  = "Outbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "VirtualNetwork"
  }
}

resource "azurerm_subnet_network_security_group_association" "edgesubnetsg" {
  subnet_id                 = azurerm_subnet.snetedge.id
  network_security_group_id = azurerm_network_security_group.snetedgesg.id
}

resource "azurerm_network_security_group" "snetdbsg" {
  name                = "snetdb-sg"
  location            = azurerm_resource_group.resourcegrp.location
  resource_group_name = azurerm_resource_group.resourcegrp.name

  security_rule {
    name                       = "allowtcp"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "5432"
    source_address_prefix      = "10.20.2.0/24"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "alldeny"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "dbsubnetsg" {
  subnet_id                 = azurerm_subnet.snetdb.id
  network_security_group_id = azurerm_network_security_group.snetdbsg.id
}

