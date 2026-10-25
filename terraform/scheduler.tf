locals {
  schedules = {
    renew-youtube     = { lambda = "subscribe-to-websub", expression = "cron(0 0 * * ? *)" }
    renew-twitch      = { lambda = "subscribe-to-eventsub", expression = "cron(0 0 * * ? *)" }
    poll-live-streams = { lambda = "get-stream-stats", expression = "rate(2 minutes)" }
  }
}
resource "aws_scheduler_schedule" "jobs" {
  for_each                     = local.schedules
  name                         = "${local.prefix}-${each.key}"
  schedule_expression          = each.value.expression
  schedule_expression_timezone = "UTC"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.fn[each.value.lambda].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ job = each.key })
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 300
    }
  }
}
