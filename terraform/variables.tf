# --- Region / naming (non-identifying; committed in uksouth.auto.tfvars) ---
variable "location" {
  description = "Azure region."
  type        = string
  default     = "uksouth"
}

variable "loc_short" {
  description = "Short region token used in resource names."
  type        = string
  default     = "uks"
}

variable "name_prefix" {
  description = "Short prefix used to name this solution's resources."
  type        = string
  default     = "creditrisk"
}

variable "resource_group_name" {
  description = "Resource group for THIS solution's own resources (deleted by `make teardown-full`)."
  type        = string
  default     = "credit-risk-monitoring-rg"
}

variable "tags" {
  description = "Tags applied to this solution's resources."
  type        = map(string)
  default     = {}
}

# --- Postgres (audit sink) ---
variable "database_name" {
  description = "Application database name (non-RAG; the postgres module default is intentionally not used)."
  type        = string
  default     = "creditrisk"
}

variable "postgres_sku_name" {
  description = "Postgres flexible server SKU."
  type        = string
  default     = "GP_Standard_D2ds_v5"
}

variable "postgres_storage_mb" {
  description = "Postgres storage in MB."
  type        = number
  default     = 32768
}

# --- Container image (registry login server injected from the ACR data source;
#     never a literal in tracked files) ---
variable "image_repository" {
  description = "Repository/name of the Arm A image within the ACR (no registry prefix)."
  type        = string
  default     = "credit-risk-monitoring"
}

variable "image_tag" {
  description = "Image tag/digest to deploy. Set to the pushed tag in Phase 2 (avoid 'latest' in prod)."
  type        = string
  default     = "latest"
}

variable "aca_max_replicas" {
  description = "Max replica count for the Arm A app (min is 0 — scale-to-zero)."
  type        = number
  default     = 1
}

variable "key_vault_public_network_access_enabled" {
  description = "Whether the solution Key Vault allows public network access. Default true so a laptop apply + secret population works and the VNet-integrated app can read over the service endpoint. Set false only once a KV private endpoint + privatelink.vaultcore DNS zone are in place (see README — scoped follow-up)."
  type        = bool
  default     = true
}

# --- Identifying values (git-ignored secrets.auto.tfvars; see .example) ---
variable "acr_name" {
  description = "Shared ACR name. Identifying — supply via git-ignored tfvars (org secret ACR_NAME)."
  type        = string
}

variable "acr_resource_group_name" {
  description = "Resource group of the shared ACR. Supply via git-ignored tfvars."
  type        = string
}

variable "aad_admin_object_id" {
  description = "Object ID of the Entra principal to register as Postgres AAD admin (a GUID — git-ignored)."
  type        = string
}

variable "aad_admin_principal_name" {
  description = "Principal name of the Postgres AAD admin."
  type        = string
}

variable "aad_admin_principal_type" {
  description = "Principal type of the Postgres AAD admin (User | Group | ServicePrincipal)."
  type        = string
  default     = "Group"
}

variable "sec_edgar_user_agent" {
  description = "Descriptive User-Agent SEC EDGAR requires (contains a contact email — git-ignored)."
  type        = string
}

# --- Secret VALUES (git-ignored; populated at deploy time from localdevenv) ---
# These land in Key Vault as this solution's OWN secrets. Kept out of tracked
# files entirely; the module never reads the shared dev vault.
variable "anthropic_api_key" {
  description = "Anthropic API key value (deploy-time; from localdevenv anthropic-portfolio-key)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "companies_house_api_key" {
  description = "Companies House API key value (deploy-time; from localdevenv credit-risk-monitoring-ch-api-key)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "braintrust_api_key" {
  description = "Optional Braintrust API key. Empty => the secret + its env var are omitted (the app no-ops Braintrust)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "audit_database_url" {
  description = "Postgres DSN for the audit sink (AUDIT_DATABASE_URL). Assembled at deploy time from the postgres outputs + credentials — see README."
  type        = string
  sensitive   = true
  default     = ""
}
