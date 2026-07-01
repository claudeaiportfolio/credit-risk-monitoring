output "resource_group_name" {
  description = "This solution's resource group (target of `make teardown-full`)."
  value       = azurerm_resource_group.this.name
}

output "key_vault_name" {
  description = "This solution's Key Vault name (populate secrets here in Phase 2)."
  value       = azurerm_key_vault.this.name
}

output "key_vault_uri" {
  description = "Key Vault URI."
  value       = azurerm_key_vault.this.vault_uri
}

output "app_identity_client_id" {
  description = "Client ID of the Container App's user-assigned identity."
  value       = azurerm_user_assigned_identity.app.client_id
}

output "app_identity_principal_id" {
  description = "Principal (object) ID of the Container App's user-assigned identity."
  value       = azurerm_user_assigned_identity.app.principal_id
}

output "image" {
  description = "Fully-qualified image ref the Container App expects (build+push this to ACR in Phase 2)."
  value       = local.image
}

output "postgres_primary_fqdn" {
  description = "Private FQDN of the Postgres primary (resolves to the private endpoint inside the VNet). Use to assemble AUDIT_DATABASE_URL."
  value       = module.postgres.primary_fqdn
}

output "postgres_database_name" {
  description = "Audit database name."
  value       = module.postgres.database_name
}

output "container_app_name" {
  description = "Name of the deployed Container App (null until the aca module is re-pinned + applied)."
  value       = module.aca.container_app_name
}
