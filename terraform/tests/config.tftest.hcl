# Mock plans exercise YAML wiring without credentials or AWS requests.
mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      address = "postgres.internal"
      master_user_secret = [{
        secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"
      }]
    }
  }
}

run "defaults_and_shared_bindings" {
  command = plan

  assert {
    condition     = aws_scheduler_schedule.jobs["renew-youtube"].schedule_expression == local.infrastructure.schedules.jobs.renew-youtube.expression && aws_scheduler_schedule.jobs["poll-live-streams"].schedule_expression == local.infrastructure.schedules.jobs.poll-live-streams.expression
    error_message = "Schedules must follow the configured maintenance and polling intervals."
  }
  assert {
    condition     = aws_scheduler_schedule.jobs["update-channel-history"].schedule_expression == "cron(0 * * * ? *)" && aws_scheduler_schedule.jobs["update-channel-history"].schedule_expression_timezone == "UTC"
    error_message = "Channel history must have an hourly UTC cron schedule."
  }
  assert {
    condition     = jsondecode(aws_scheduler_schedule.jobs["update-channel-history"].target[0].input).scheduled_at == "<aws.scheduler.scheduled-time>" && contains(local.infrastructure.lambda.async_functions, "update-channels")
    error_message = "Channel updates must receive stable schedule times and asynchronous failure handling."
  }
  assert {
    condition     = alltrue([for schedule in aws_scheduler_schedule.jobs : strcontains(schedule.target[0].input, "<aws.scheduler.scheduled-time>")])
    error_message = "Scheduler time placeholders must retain literal angle brackets in the raw target input so AWS can substitute them."
  }
  assert {
    condition     = alltrue([for schedule in aws_scheduler_schedule.jobs : schedule.state == "DISABLED"])
    error_message = "Schedules must remain disabled by default."
  }
  assert {
    condition     = aws_db_instance.postgres.instance_class == "db.t4g.micro" && aws_db_instance.postgres.allocated_storage == 20 && !aws_db_instance.postgres.publicly_accessible
    error_message = "The refactor must preserve database size and private access."
  }
  assert {
    condition     = aws_apigatewayv2_route.youtube_post.route_key == "POST ${local.infrastructure.api.callback_paths.youtube}" && aws_apigatewayv2_route.twitch_post.route_key == "POST ${local.infrastructure.api.callback_paths.twitch}"
    error_message = "API routes must use the same configured paths as callbacks."
  }
  assert {
    condition     = aws_lambda_function.fn["get-stream-stats"].environment[0].variables[local.runtime_config.environment.database_port] == tostring(aws_db_instance.postgres.port)
    error_message = "The Lambda database binding must follow the configured database port."
  }
}

run "explicit_overrides" {
  command = plan
  variables {
    project                        = "custom-tracker"
    postgres_backup_retention_days = 0
    tracking_jobs_enabled          = true
  }
  assert {
    condition     = aws_db_instance.postgres.backup_retention_period == 0 && startswith(aws_db_instance.postgres.identifier, "custom-tracker-")
    error_message = "Explicit Terraform overrides, including zero, must win over YAML defaults."
  }
  assert {
    condition     = alltrue([for schedule in aws_scheduler_schedule.jobs : schedule.state == "ENABLED"])
    error_message = "Explicit tracking enablement must override the YAML default."
  }
}
