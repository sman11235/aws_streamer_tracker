terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
provider "aws" {
  region = local.settings.aws_region
  default_tags { tags = { Project = local.settings.project, Environment = local.settings.environment, ManagedBy = "Terraform" } }
}
