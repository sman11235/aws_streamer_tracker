resource "aws_cloudwatch_event_bus" "stream_events" {
  name = "${local.prefix}-event-bus"
}
resource "aws_cloudwatch_event_rule" "stream_changes" {
  name           = "${local.prefix}-stream-changes"
  event_bus_name = aws_cloudwatch_event_bus.stream_events.name
  event_pattern = jsonencode({
    source        = ["streamer.youtube", "streamer.twitch"],
    "detail-type" = ["StreamLifecycleEvent"]
  })
}
resource "aws_cloudwatch_event_target" "stream_handler" {
  rule           = aws_cloudwatch_event_rule.stream_changes.name
  event_bus_name = aws_cloudwatch_event_bus.stream_events.name
  target_id      = "handle-stream-events"
  arn            = aws_lambda_function.fn["handle-stream-events"].arn
}
resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeBusRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["handle-stream-events"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.stream_changes.arn
}
