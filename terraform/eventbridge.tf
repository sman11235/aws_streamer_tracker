resource "aws_cloudwatch_event_bus" "stream_events" {
  name = "${local.prefix}-event-bus"
}
resource "aws_cloudwatch_event_rule" "stream_changes" {
  name           = "${local.prefix}-stream-changes"
  event_bus_name = aws_cloudwatch_event_bus.stream_events.name
  event_pattern = jsonencode({
    source        = [for provider in ["youtube", "twitch"] : "${local.runtime_config.events.source_prefix}${provider}"],
    "detail-type" = [local.runtime_config.events.detail_type]
  })
}
resource "aws_cloudwatch_event_target" "stream_handler" {
  rule           = aws_cloudwatch_event_rule.stream_changes.name
  event_bus_name = aws_cloudwatch_event_bus.stream_events.name
  target_id      = "handle-stream-events"
  arn            = aws_lambda_function.fn["handle-stream-events"].arn
  retry_policy {
    maximum_event_age_in_seconds = local.infrastructure.delivery.eventbridge.max_event_age_seconds
    maximum_retry_attempts       = local.infrastructure.delivery.eventbridge.max_retry_attempts
  }
  dead_letter_config {
    arn = aws_sqs_queue.ingestion_failures.arn
  }
  depends_on = [aws_sqs_queue_policy.ingestion_failures]
}
resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeBusRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["handle-stream-events"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.stream_changes.arn
}
