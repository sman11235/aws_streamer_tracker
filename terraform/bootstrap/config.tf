locals {
  bootstrap = yamldecode(file("${path.module}/../../config/bootstrap.yaml"))
  oidc_host = trimprefix(local.bootstrap.oidc.issuer_url, "https://")
  settings = {
    aws_region                 = var.aws_region != null ? var.aws_region : local.bootstrap.variables.aws_region
    project                    = var.project != null ? var.project : local.bootstrap.variables.project
    github_repository          = var.github_repository != null ? var.github_repository : local.bootstrap.variables.github_repository
    github_owner_id            = var.github_owner_id != null ? var.github_owner_id : local.bootstrap.variables.github_owner_id
    github_repository_id       = var.github_repository_id != null ? var.github_repository_id : local.bootstrap.variables.github_repository_id
    existing_oidc_provider_arn = var.existing_oidc_provider_arn != null ? var.existing_oidc_provider_arn : local.bootstrap.variables.existing_oidc_provider_arn
  }
}
