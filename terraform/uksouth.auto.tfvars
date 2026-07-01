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
aca_max_replicas = 1

tags = {
  solution    = "credit-risk-monitoring"
  arm         = "arm-a"
  managed-by  = "terraform"
  environment = "prod"
}
