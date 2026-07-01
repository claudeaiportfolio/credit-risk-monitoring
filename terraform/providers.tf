provider "azurerm" {
  features {}

  # OIDC by default (CI / federated). For a laptop apply, drop in the
  # git-ignored local_override.tf (see local_override.tf.example) to switch to
  # use_cli. subscription_id comes from ARM_SUBSCRIPTION_ID (never committed).
  use_oidc = true
}

provider "random" {}
