# Arm A (C3b) production hosting: this solution's OWN resource group, Key Vault,
# managed identity, Postgres (private-endpoint) audit sink, and the Azure
# Container Apps deployment of the investigation agent. Only module INVOCATIONS
# + local resources live here — module bodies live in portfolio-infra.

data "azurerm_client_config" "current" {}

# Shared ACR (name is identifying -> supplied via git-ignored tfvars). We read
# it to (a) scope the AcrPull grant and (b) derive the login server for the
# image + registry auth, so no ACR literal lands in tracked files.
data "azurerm_container_registry" "shared" {
  name                = var.acr_name
  resource_group_name = var.acr_resource_group_name
}

resource "azurerm_resource_group" "this" {
  name     = var.resource_group_name
  location = var.location
  tags     = var.tags
}

# --- Managed identity for the Container App --------------------------------
# A PLAIN user-assigned identity (no OIDC federation). The shared `identity`
# module is intentionally not used here: it federates UAMIs to Kubernetes
# ServiceAccounts (namespace/sa_name) for AKS workload identity, which does not
# apply to Azure Container Apps. Deviation documented here + in the README.
resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.name_prefix}-app-${var.loc_short}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = var.tags
}

# --- Observability ---------------------------------------------------------
resource "azurerm_log_analytics_workspace" "this" {
  name                = "${var.name_prefix}-log-${var.loc_short}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = var.tags
}

# --- Key Vault (this solution's OWN vault — not the shared dev vault) -------
resource "random_string" "kv_suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_key_vault" "this" {
  name                = "${var.name_prefix}-kv-${var.loc_short}-${random_string.kv_suffix.result}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  # RBAC (not access policies) — grants are explicit role assignments below.
  rbac_authorization_enabled = true

  # Bring-up/teardown lifecycle: no purge protection so `make teardown-full`
  # can delete the RG and a later re-apply is not blocked by a soft-deleted
  # vault (same class as the AOAI soft-delete gotcha).
  purge_protection_enabled   = false
  soft_delete_retention_days = 7

  public_network_access_enabled = var.key_vault_public_network_access_enabled

  tags = var.tags
}

# The deploying principal needs write access to populate secrets (Phase 2).
resource "azurerm_role_assignment" "deployer_kv_officer" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# The app's identity reads secrets at runtime.
resource "azurerm_role_assignment" "app_kv_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# The app's identity pulls the image from the shared ACR.
resource "azurerm_role_assignment" "app_acr_pull" {
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

locals {
  # Secret NAME (in KV) -> value. Values are populated at deploy time from the
  # git-ignored secrets.auto.tfvars (sourced from localdevenv). Only secrets
  # with a value are created/wired, so `validate`/`plan` work with none set and
  # a single `apply` wires them all once the tfvars is filled.
  secret_values = {
    "anthropic-api-key"       = var.anthropic_api_key
    "companies-house-api-key" = var.companies_house_api_key
    "audit-database-url"      = var.audit_database_url
    "braintrust-api-key"      = var.braintrust_api_key
  }
  active_secrets = { for k, v in local.secret_values : k => v if v != "" }
  # Secret NAMES are not sensitive (only their values are), but building the map
  # from sensitive vars marks the whole collection sensitive — and Terraform
  # forbids a sensitive `for_each`. Unwrap just the key set so it can drive
  # for_each; the looked-up values stay sensitive. (Whether an optional secret
  # is present is not meaningful leakage.)
  active_secret_keys = nonsensitive(toset(keys(local.active_secrets)))

  # KV secret name -> the env var the app reads it as.
  secret_env_name = {
    "anthropic-api-key"       = "ANTHROPIC_API_KEY"
    "companies-house-api-key" = "COMPANIES_HOUSE_API_KEY"
    "audit-database-url"      = "AUDIT_DATABASE_URL"
    "braintrust-api-key"      = "BRAINTRUST_API_KEY"
  }
  secret_env_vars = { for k in local.active_secret_keys : local.secret_env_name[k] => k }

  image = "${data.azurerm_container_registry.shared.login_server}/${var.image_repository}:${var.image_tag}"
}

resource "azurerm_key_vault_secret" "this" {
  for_each     = local.active_secret_keys
  name         = each.key
  value        = local.active_secrets[each.key]
  key_vault_id = azurerm_key_vault.this.id

  # Wait for the deployer's write grant before creating secrets. (RBAC role
  # propagation can lag a minute; re-run apply if the first create races it.)
  depends_on = [azurerm_role_assignment.deployer_kv_officer]
}

# --- Postgres audit sink (private endpoint, real — enable_private_endpoints) -
module "postgres" {
  source = "git::https://github.com/claudeaiportfolio/portfolio-infra.git//terraform/modules/postgres?ref=tf-modules-v0.2.0"

  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  name_prefix         = var.name_prefix
  loc_short           = var.loc_short
  sku_name            = var.postgres_sku_name
  storage_mb          = var.postgres_storage_mb

  tenant_id                = data.azurerm_client_config.current.tenant_id
  aad_admin_object_id      = var.aad_admin_object_id
  aad_admin_principal_name = var.aad_admin_principal_name
  aad_admin_principal_type = var.aad_admin_principal_type

  database_name = var.database_name # non-RAG name via tfvars, not the module default

  # Real private endpoints wired to the SHARED network (no facade).
  enable_private_endpoints   = true
  private_endpoint_subnet_id = local.private_endpoint_subnet_id
  private_dns_zone_id        = local.postgres_dns_zone_id

  tags = var.tags
}

# --- Arm A on Azure Container Apps (a JOB) ---------------------------------
# Arm A is EPISODIC: the entrypoint (`credit-risk-eval run-agent`) runs one
# investigation to completion and exits. The correct primitive is a Container
# App JOB with a manual (on-demand) trigger — a scale-to-zero Container App with
# no ingress would deploy but never wake (a facade). An operator starts a run
# with `az containerapp job start` (which is also how it is smoke-tested).
module "aca" {
  source = "git::https://github.com/claudeaiportfolio/portfolio-infra.git//terraform/modules/aca?ref=tf-modules-v0.3.0"

  workload_kind = "job"

  resource_group_name = azurerm_resource_group.this.name
  location            = var.location
  name_prefix         = var.name_prefix
  loc_short           = var.loc_short

  # VNet-integrated on the shared ACA subnet, internal environment (no public
  # exposure). Manual on-demand trigger; one run per execution.
  infrastructure_subnet_id       = local.aca_subnet_id
  internal_ingress_only          = true
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.this.id
  job_replica_timeout_in_seconds = var.job_replica_timeout_in_seconds
  job_replica_retry_limit        = var.job_replica_retry_limit
  job_parallelism                = 1
  job_replica_completion_count   = 1

  image                     = local.image
  registry_server           = data.azurerm_container_registry.shared.login_server
  user_assigned_identity_id = azurerm_user_assigned_identity.app.id

  secrets = [
    for k, s in azurerm_key_vault_secret.this : {
      name                = k
      key_vault_secret_id = s.versionless_id
    }
  ]
  secret_env_vars = local.secret_env_vars
  env_vars        = { SEC_EDGAR_USER_AGENT = var.sec_edgar_user_agent }

  tags = var.tags

  # The first job execution reads the KV secrets -> the app identity must
  # already hold Secrets User and the secrets must exist.
  depends_on = [
    azurerm_role_assignment.app_kv_secrets_user,
    azurerm_key_vault_secret.this,
  ]
}
