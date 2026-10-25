variable "aws_region" {
  description = "AWS Region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Prefix for resource names."
  type        = string
  default     = "streamer-stats"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "youtube_websub_secret_parameter" {
  description = "Existing SSM SecureString parameter name containing the YouTube WebSub signing secret."
  type        = string
  default     = "/streamer-stats/youtube-websub-secret"
}

variable "twitch_secret_parameter" {
  description = "Existing SSM SecureString parameter name containing the Twitch EventSub signing secret."
  type        = string
  default     = "/streamer-stats/twitch-eventsub-secret"
}

variable "twitch_client_id_parameter" {
  description = "Existing SSM SecureString parameter containing Twitch client ID."
  type        = string
  default     = "/streamer-stats/twitch-client-id"
}

variable "twitch_client_secret_parameter" {
  description = "Existing SSM SecureString parameter containing Twitch app client secret."
  type        = string
  default     = "/streamer-stats/twitch-client-secret"
}

variable "vpc_cidr" {
  description = "CIDR block for the dedicated application VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "postgres_database_name" {
  description = "Initial PostgreSQL database name."
  type        = string
  default     = "streamer_tracker"
}

variable "postgres_master_username" {
  description = "RDS PostgreSQL master username; its password is managed by RDS in Secrets Manager."
  type        = string
  default     = "streamer_admin"
}

variable "postgres_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "postgres_engine_version" {
  description = "PostgreSQL engine version."
  type        = string
  default     = "17.6"
}

variable "lambda_artifact_directory" {
  description = "Directory containing your six prebuilt Lambda ZIP files, relative to this Terraform folder or absolute."
  type        = string
  default     = "artifacts"
}
