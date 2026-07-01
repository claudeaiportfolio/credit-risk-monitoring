# Region + non-identifying config for this solution (auto-loaded, committed).
# Identifying values (GUIDs, ACR name, contact email, secret values) live in a
# git-ignored secrets.auto.tfvars — see secrets.auto.tfvars.example.

location    = "uksouth"
loc_short   = "uks"
name_prefix = "creditrisk"

resource_group_name = "credit-risk-monitoring-rg"

database_name       = "creditrisk"
postgres_sku_name   = "GP_Standard_D2ds_v5"
postgres_storage_mb = 32768

image_repository = "credit-risk-monitoring"

# Arm A runs as an on-demand Container App Job (episodic run-to-completion).
job_replica_timeout_in_seconds = 3600
job_replica_retry_limit        = 1

tags = {
  solution    = "credit-risk-monitoring"
  arm         = "arm-a"
  managed-by  = "terraform"
  environment = "prod"
}
