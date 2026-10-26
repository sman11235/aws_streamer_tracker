# Run once with local AWS credentials before enabling the deployment workflow.
# This root has its own state, independent of the application state bucket.
terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = local.settings.aws_region
  default_tags {
    tags = {
      Project     = local.settings.project
      Environment = local.bootstrap.environment
      ManagedBy   = "Terraform"
    }
  }
}

variable "aws_region" {
  type    = string
  default = null
}

variable "project" {
  type    = string
  default = null
}

variable "github_repository" {
  description = "GitHub owner/repository allowed to deploy."
  type        = string
  default     = null
}

variable "github_owner_id" {
  description = "Immutable numeric GitHub owner ID used in the OIDC subject."
  type        = string
  default     = null
}

variable "github_repository_id" {
  description = "Immutable numeric GitHub repository ID used in the OIDC subject."
  type        = string
  default     = null
}

variable "existing_oidc_provider_arn" {
  description = "Reuse the account's GitHub OIDC provider if one already exists."
  type        = string
  default     = null
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  prefix    = "${local.settings.project}-${local.bootstrap.environment}"
  state_key = "${local.settings.project}/${local.bootstrap.environment}/terraform.tfstate"
  oidc_arn  = local.settings.existing_oidc_provider_arn != null ? local.settings.existing_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn

  github_repository_parts = split("/", local.settings.github_repository)
  github_oidc_subject     = "repo:${local.github_repository_parts[0]}@${local.settings.github_owner_id}/${local.github_repository_parts[1]}@${local.settings.github_repository_id}:environment:${local.bootstrap.environment}"
}

resource "aws_s3_bucket" "state" {
  bucket = "${local.prefix}-${data.aws_caller_identity.current.account_id}-${local.settings.aws_region}-tfstate"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = local.settings.existing_oidc_provider_arn == null ? 1 : 0
  url            = local.bootstrap.oidc.issuer_url
  client_id_list = [local.bootstrap.oidc.audience]
}

resource "aws_iam_role" "deploy" {
  name = "${local.prefix}-github-actions"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_host}:aud" = local.bootstrap.oidc.audience
          "${local.oidc_host}:sub" = local.github_oidc_subject
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "state" {
  name = "terraform-state"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.state.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.state.arn}/${local.state_key}"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.state.arn}/${local.state_key}.tflock"
      }
    ]
  })
}

resource "aws_iam_role_policy" "infrastructure" {
  name = "application-infrastructure"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:*", "rds:*", "lambda:*", "events:*", "scheduler:*",
          "logs:*", "apigateway:*", "sqs:*", "cloudwatch:*", "kms:DescribeKey",
          "secretsmanager:CreateSecret", "secretsmanager:DeleteSecret",
          "secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy",
          "secretsmanager:ListSecretVersionIds", "secretsmanager:TagResource",
          "secretsmanager:UntagResource"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole",
          "iam:UpdateAssumeRolePolicy", "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies", "iam:GetRolePolicy",
          "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.prefix}-*-role"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
        Resource = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.prefix}-*-role"
        Condition = {
          ArnEquals = {
            "iam:PolicyARN" = [
              "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
              "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
            ]
          }
        }
      },
      {
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.prefix}-*-role"
        Condition = { StringEquals = { "iam:PassedToService" = ["lambda.amazonaws.com", "scheduler.amazonaws.com"] } }
      },
      {
        Effect    = "Allow"
        Action    = ["iam:CreateServiceLinkedRole"]
        Resource  = "*"
        Condition = { StringEquals = { "iam:AWSServiceName" = "rds.amazonaws.com" } }
      }
    ]
  })
}

output "github_environment_variables" {
  description = "Set these variables in the repository's dev GitHub environment."
  value = {
    AWS_ROLE_ARN    = aws_iam_role.deploy.arn
    AWS_REGION      = local.settings.aws_region
    PROJECT         = local.settings.project
    TF_STATE_BUCKET = aws_s3_bucket.state.id
    TF_STATE_KEY    = local.state_key
  }
}

output "github_oidc_subject" {
  description = "Expected GitHub OIDC subject in the deployment role trust policy."
  value       = local.github_oidc_subject
}
