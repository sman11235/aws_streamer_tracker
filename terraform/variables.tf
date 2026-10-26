variable "aws_region" {
  description = "AWS Region for all resources."
  type        = string
  default     = null
}

variable "project" {
  description = "Prefix for resource names."
  type        = string
  default     = null
}

variable "environment" {
  description = "Environment name (dev, staging, prod)."
  type        = string
  default     = null
}

variable "youtube_websub_secret_parameter" {
  description = "Existing SSM SecureString parameter name containing the YouTube WebSub signing secret."
  type        = string
  default     = null
}

variable "twitch_secret_parameter" {
  description = "Existing SSM SecureString parameter name containing the Twitch EventSub signing secret."
  type        = string
  default     = null
}

variable "twitch_client_id_parameter" {
  description = "Existing SSM SecureString parameter containing Twitch client ID."
  type        = string
  default     = null
}

variable "twitch_client_secret_parameter" {
  description = "Existing SSM SecureString parameter containing Twitch app client secret."
  type        = string
  default     = null
}

variable "youtube_api_key_parameter" {
  description = "SSM SecureString parameter containing a YouTube Data API v3 key for public video metadata."
  type        = string
  default     = null
}

variable "tracking_jobs_enabled" {
  description = "Enable scheduled ingestion after applying database migrations and creating SSM parameters."
  type        = bool
  default     = null
}

variable "alarm_topic_arn" {
  description = "Optional existing SNS topic ARN for ingestion error and dead-letter alerts."
  type        = string
  default     = null
}

variable "vpc_cidr" {
  description = "CIDR block for the dedicated application VPC."
  type        = string
  default     = null
}

variable "postgres_database_name" {
  description = "Initial PostgreSQL database name."
  type        = string
  default     = null
}

variable "postgres_master_username" {
  description = "RDS PostgreSQL master username; its password is managed by RDS in Secrets Manager."
  type        = string
  default     = null
}

variable "postgres_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = null
}

variable "postgres_engine_version" {
  description = "PostgreSQL engine version."
  type        = string
  default     = null
}

variable "postgres_backup_retention_days" {
  description = "Days to retain automated backups (0 disables them). Your AWS account plan may limit the maximum."
  type        = number
  default     = null

  validation {
    condition     = var.postgres_backup_retention_days == null ? true : var.postgres_backup_retention_days >= 0 && var.postgres_backup_retention_days <= 35 && floor(var.postgres_backup_retention_days) == var.postgres_backup_retention_days
    error_message = "Backup retention must be a whole number between 0 and 35 days."
  }
}

variable "lambda_artifact_directory" {
  description = "Directory containing prebuilt Lambda ZIP files, relative to this Terraform folder or absolute."
  type        = string
  default     = null
}
