# Read the SHARED network outputs (VNet, subnets, private DNS zones) from the
# portfolio-infra root state rather than hardcoding their resource IDs — those
# IDs embed the subscription GUID (public-repo rule). Only the backend *config*
# (storage account / RG / container / key names) is referenced here; it carries
# no GUID or secret, and mirrors the codebase's existing precedent of naming
# shared resources in tracked files (see portfolio-infra terraform/data.tf,
# which names the shared KV + its RG).
data "terraform_remote_state" "shared" {
  backend = "azurerm"

  config = {
    resource_group_name  = "claudeaiportfolio"
    storage_account_name = "localtfsa"
    container_name       = "tfstate"
    key                  = "auth0.tfstate"
  }
}

locals {
  aca_subnet_id              = data.terraform_remote_state.shared.outputs.aca_subnet_id
  private_endpoint_subnet_id = data.terraform_remote_state.shared.outputs.private_endpoint_subnet_id
  postgres_dns_zone_id       = data.terraform_remote_state.shared.outputs.private_dns_zone_ids["postgres"]
}
