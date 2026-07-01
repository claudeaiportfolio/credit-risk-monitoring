# Arm A production hosting (C3b) — Terraform

Deploys the Arm A investigation agent to **Azure Container Apps**, plus this
solution's own **Key Vault**, **managed identity**, and a private-endpoint
**Postgres** audit sink. Per portfolio convention this dir holds only module
**invocations** + wiring; the module bodies live in `portfolio-infra`.

## What it provisions

| Resource | Notes |
|----------|-------|
| `azurerm_resource_group.this` | This solution's OWN RG — deleted by `make teardown-full`. |
| `azurerm_user_assigned_identity.app` | Plain UAMI for the Container App (ACR pull + KV secret reads). |
| `azurerm_log_analytics_workspace.this` | Container App environment logs. |
| `azurerm_key_vault.this` (+ RBAC role assignments) | This solution's OWN vault. UAMI gets **Key Vault Secrets User**; the deployer gets **Secrets Officer**. |
| `azurerm_key_vault_secret.this` | The app's secrets (values from git-ignored tfvars at deploy time). |
| `azurerm_role_assignment.app_acr_pull` | UAMI **AcrPull** on the shared ACR. |
| `module.postgres` (`tf-modules-v0.2.0`) | Flexible server + replica, **real private endpoint** (`enable_private_endpoints = true`) wired to the shared network. DB name `creditrisk` (not the module's default). |
| `module.aca` (`tf-modules-vNEXT` — re-pin) | Arm A as a **Container App Job** (`workload_kind = "job"`, manual/on-demand trigger): VNet-integrated on the shared `aca-environment` subnet, internal environment (no public exposure), image from ACR, secrets from KV via the UAMI. |

The app reads (all from `os.environ`): `ANTHROPIC_API_KEY`,
`COMPANIES_HOUSE_API_KEY`, `AUDIT_DATABASE_URL`, optional `BRAINTRUST_API_KEY`
(all KV secret refs), and `SEC_EDGAR_USER_AGENT` (plain env).

## Design decisions & documented deviations

- **Arm A deploys as a Container App JOB, not a Container App** (revision of an
  earlier "ACA app" shorthand). Arm A is **episodic**: the entrypoint
  (`credit-risk-eval run-agent`) runs one investigation to completion and exits.
  A scale-to-zero Container App with no ingress would deploy green but never
  wake — a facade. A **Job** with a manual (on-demand) trigger is the correct
  primitive, and lets an operator actually start (and smoke-test) a real run via
  `az containerapp job start`. Scheduled/event triggers are a future option in
  the module; Arm A uses manual.
- **Own Key Vault, not the shared dev vault.** Production ACA must not depend on
  the shared `localdevenv` vault. This provisions a per-solution KV; secret
  *values* are copied in at deploy time (Phase 2) from `localdevenv`
  (`anthropic-portfolio-key`, `credit-risk-monitoring-ch-api-key`,
  `credit-risk-monitoring-braintrust`).
- **Secret values pass through Terraform state** (KV-secrets-from-variables).
  Chosen for a single clean `apply` and so the job's first execution can read
  the refs. State lives in the private backend storage account. *Deviation from
  "no secrets in code":* values are in git-ignored `secrets.auto.tfvars`, never
  tracked; the tradeoff is state-resident secrets.
- **Plain UAMI, not the shared `identity` module.** That module federates UAMIs
  to Kubernetes ServiceAccounts (AKS workload identity) — not applicable to
  Container Apps, which reference the UAMI directly.
- **Key Vault public network access defaults ON.** Lets a laptop apply populate
  secrets and lets the VNet-integrated app read over the service endpoint. A
  fully-private KV needs a `privatelink.vaultcore.azure.net` DNS zone (the
  shared network only ships postgres/blob/openai zones today) + a KV private
  endpoint. **Scoped follow-up**, tracked by
  `key_vault_public_network_access_enabled`.
- **No KV purge protection.** So `make teardown-full` + a later re-apply is not
  blocked by a soft-deleted vault (same class as the AOAI soft-delete gotcha).
- **Shared network IDs via `terraform_remote_state`** (never hardcoded — they
  embed the subscription GUID). The remote-state backend *config* (SA/RG names)
  is referenced literally, consistent with `portfolio-infra/terraform/data.tf`.

## Files

- `uksouth.auto.tfvars` — committed; region + non-identifying config.
- `secrets.auto.tfvars.example` — copy to `secrets.auto.tfvars` (git-ignored);
  all identifying + secret values.
- `local_override.tf.example` — copy to `local_override.tf` (git-ignored) for a
  laptop apply (`use_cli = true`).

## Phase 2 runbook (human — after Deliverable 1 is merged + tagged)

1. **Tag** `portfolio-infra` at the `aca` PR merge as `tf-modules-v0.3.0`.
2. **Re-pin** `module.aca` `ref=tf-modules-vNEXT` → `ref=tf-modules-v0.3.0` in
   `main.tf`.
3. **Build + push** the image to ACR (see repo root `Dockerfile`):
   ```
   az acr login -n <ACR_NAME>
   docker build -t <acr>.azurecr.io/credit-risk-monitoring:<tag> .
   docker push  <acr>.azurecr.io/credit-risk-monitoring:<tag>
   ```
   Set `image_tag = "<tag>"` in `secrets.auto.tfvars`.
4. **Configure**: copy both `.example` files and fill them. Export:
   ```
   export ARM_SUBSCRIPTION_ID=<sub-guid>
   az login   # for the use_cli override
   ```
5. **Init** with the backend config (nothing identifying is committed):
   ```
   terraform init \
     -backend-config="resource_group_name=claudeaiportfolio" \
     -backend-config="storage_account_name=localtfsa" \
     -backend-config="container_name=tfstate" \
     -backend-config="key=credit-risk-monitoring.tfstate"
   ```
6. **Apply**: `terraform plan` then `terraform apply`. Postgres generates its
   admin password but does not output it — reset it (or create a dedicated app
   role), assemble `AUDIT_DATABASE_URL`
   (`postgresql://<user>:<pass>@<postgres_primary_fqdn>:5432/creditrisk?sslmode=require`),
   set it in `secrets.auto.tfvars`, and re-apply so the KV secret + job env are
   wired. (If the very first secret create races RBAC propagation, just re-run.)
7. **Smoke**: start an on-demand execution and watch it run to completion:
   ```
   az containerapp job start --name "$(terraform output -raw job_name)" \
     --resource-group "$(terraform output -raw resource_group_name)"
   az containerapp job execution list --name "$(terraform output -raw job_name)" \
     --resource-group "$(terraform output -raw resource_group_name)" -o table
   ```
   Confirm the run reads its KV secrets and reaches Postgres over the private
   endpoint (check the job execution logs in Log Analytics).
8. **Teardown**: `make teardown-full` (deletes THIS RG + `az aks stop` the
   shared cluster; never deletes shared infra).

## Backend config values (for `-backend-config`)

| Key | Value |
|-----|-------|
| `resource_group_name` | `claudeaiportfolio` |
| `storage_account_name` | `localtfsa` |
| `container_name` | `tfstate` |
| `key` | `credit-risk-monitoring.tfstate` |
