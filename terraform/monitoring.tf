resource "aws_sqs_queue" "ingestion_failures" {
  name                      = "${local.prefix}-ingestion-failures"
  message_retention_seconds = local.infrastructure.delivery.failure_queue_retention_seconds
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue_policy" "ingestion_failures" {
  queue_url = aws_sqs_queue.ingestion_failures.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.ingestion_failures.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.stream_changes.arn } }
    }]
  })
}

resource "aws_lambda_function_event_invoke_config" "ingestion" {
  for_each = toset(local.infrastructure.lambda.async_functions)

  function_name                = aws_lambda_function.fn[each.key].function_name
  maximum_retry_attempts       = local.infrastructure.delivery.lambda_async.max_retry_attempts
  maximum_event_age_in_seconds = local.infrastructure.delivery.lambda_async.max_event_age_seconds
  destination_config {
    on_failure {
      destination = aws_sqs_queue.ingestion_failures.arn
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = local.functions

  alarm_name          = "${local.prefix}-${each.key}-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = local.infrastructure.monitoring.period_seconds
  evaluation_periods  = local.infrastructure.monitoring.evaluation_periods
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = local.infrastructure.monitoring.threshold
  treat_missing_data  = local.infrastructure.monitoring.treat_missing_data
  dimensions          = { FunctionName = aws_lambda_function.fn[each.key].function_name }
  alarm_actions       = local.settings.alarm_topic_arn == "" ? [] : [local.settings.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "ingestion_failures" {
  alarm_name          = "${local.prefix}-ingestion-failures"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = local.infrastructure.monitoring.period_seconds
  evaluation_periods  = local.infrastructure.monitoring.evaluation_periods
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = local.infrastructure.monitoring.threshold
  treat_missing_data  = local.infrastructure.monitoring.treat_missing_data
  dimensions          = { QueueName = aws_sqs_queue.ingestion_failures.name }
  alarm_actions       = local.settings.alarm_topic_arn == "" ? [] : [local.settings.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "webhook_errors" {
  alarm_name          = "${local.prefix}-webhook-server-errors"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  statistic           = "Sum"
  period              = local.infrastructure.monitoring.period_seconds
  evaluation_periods  = local.infrastructure.monitoring.evaluation_periods
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = local.infrastructure.monitoring.threshold
  treat_missing_data  = local.infrastructure.monitoring.treat_missing_data
  dimensions          = { ApiId = aws_apigatewayv2_api.api.id }
  alarm_actions       = local.settings.alarm_topic_arn == "" ? [] : [local.settings.alarm_topic_arn]
}
