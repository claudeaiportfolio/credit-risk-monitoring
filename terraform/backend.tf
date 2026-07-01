terraform {
  required_version = ">= 1.5"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Backend config is supplied at `init` time via -backend-config (mirrors the
  # portfolio-infra root). Nothing identifying is committed here. State key:
  #   credit-risk-monitoring.tfstate
  backend "azurerm" {}
}
