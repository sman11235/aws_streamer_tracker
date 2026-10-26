# YAML files are the single source of defaults; CLI / TF_VAR inputs remain overrides.
locals {
  infrastructure = yamldecode(file("${path.module}/../config/infrastructure.yaml"))
  runtime_config = yamldecode(file("${path.module}/../config/runtime.yaml"))
  build_config   = yamldecode(file("${path.module}/../scripts/build-config.yaml"))
  settings = {
    aws_region                      = var.aws_region != null ? var.aws_region : local.infrastructure.variables.aws_region
    project                         = var.project != null ? var.project : local.infrastructure.variables.project
    environment                     = var.environment != null ? var.environment : local.infrastructure.variables.environment
    youtube_websub_secret_parameter = var.youtube_websub_secret_parameter != null ? var.youtube_websub_secret_parameter : local.infrastructure.variables.youtube_websub_secret_parameter
    twitch_secret_parameter         = var.twitch_secret_parameter != null ? var.twitch_secret_parameter : local.infrastructure.variables.twitch_secret_parameter
    twitch_client_id_parameter      = var.twitch_client_id_parameter != null ? var.twitch_client_id_parameter : local.infrastructure.variables.twitch_client_id_parameter
    twitch_client_secret_parameter  = var.twitch_client_secret_parameter != null ? var.twitch_client_secret_parameter : local.infrastructure.variables.twitch_client_secret_parameter
    youtube_api_key_parameter       = var.youtube_api_key_parameter != null ? var.youtube_api_key_parameter : local.infrastructure.variables.youtube_api_key_parameter
    tracking_jobs_enabled           = var.tracking_jobs_enabled != null ? var.tracking_jobs_enabled : local.infrastructure.variables.tracking_jobs_enabled
    alarm_topic_arn                 = var.alarm_topic_arn != null ? var.alarm_topic_arn : local.infrastructure.variables.alarm_topic_arn
    vpc_cidr                        = var.vpc_cidr != null ? var.vpc_cidr : local.infrastructure.variables.vpc_cidr
    postgres_database_name          = var.postgres_database_name != null ? var.postgres_database_name : local.infrastructure.variables.postgres_database_name
    postgres_master_username        = var.postgres_master_username != null ? var.postgres_master_username : local.infrastructure.variables.postgres_master_username
    postgres_instance_class         = var.postgres_instance_class != null ? var.postgres_instance_class : local.infrastructure.variables.postgres_instance_class
    postgres_engine_version         = var.postgres_engine_version != null ? var.postgres_engine_version : local.infrastructure.variables.postgres_engine_version
    postgres_backup_retention_days  = var.postgres_backup_retention_days != null ? var.postgres_backup_retention_days : local.infrastructure.variables.postgres_backup_retention_days
    lambda_artifact_directory       = var.lambda_artifact_directory != null ? var.lambda_artifact_directory : local.infrastructure.variables.lambda_artifact_directory
  }
}
