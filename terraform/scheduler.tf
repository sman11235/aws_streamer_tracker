locals {
  schedules = local.infrastructure.schedules.jobs
}
resource "aws_scheduler_schedule" "jobs" {
  for_each                     = local.schedules
  name                         = "${local.prefix}-${each.key}"
  schedule_expression          = each.value.expression
  schedule_expression_timezone = local.infrastructure.schedules.timezone
  state                        = local.settings.tracking_jobs_enabled ? "ENABLED" : "DISABLED"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.fn[each.value.lambda].arn
    role_arn = aws_iam_role.scheduler.arn
    # Scheduler substitutes literal context tokens before the target decodes JSON.
    # jsonencode escapes angle brackets, so restore this token in the raw input.
    input = replace(
      jsonencode({ job = each.key, scheduled_at = "<aws.scheduler.scheduled-time>" }),
      "\\u003caws.scheduler.scheduled-time\\u003e",
      "<aws.scheduler.scheduled-time>"
    )
    dead_letter_config {
      arn = aws_sqs_queue.ingestion_failures.arn
    }
    retry_policy {
      maximum_retry_attempts       = local.infrastructure.delivery.scheduler.max_retry_attempts
      maximum_event_age_in_seconds = local.infrastructure.delivery.scheduler.max_event_age_seconds
    }
  }
  depends_on = [aws_iam_role_policy.scheduler, aws_apigatewayv2_stage.api, aws_apigatewayv2_route.youtube_get, aws_apigatewayv2_route.youtube_post, aws_apigatewayv2_route.twitch_post]
}
