resource "aws_apigatewayv2_api" "api" {
  name          = "${local.prefix}-webhooks"
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_stage" "api" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format          = jsonencode({ requestId = "$context.requestId", status = "$context.status", routeKey = "$context.routeKey", integrationError = "$context.integrationErrorMessage" })
  }
}
resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/apigateway/${local.prefix}-webhooks"
  retention_in_days = local.infrastructure.api.log_retention_days
}
resource "aws_apigatewayv2_integration" "youtube" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.fn["handle-youtube-notifs"].invoke_arn
  payload_format_version = local.infrastructure.api.payload_format_version
}
resource "aws_apigatewayv2_integration" "twitch" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.fn["handle-twitch-notifs"].invoke_arn
  payload_format_version = local.infrastructure.api.payload_format_version
}
resource "aws_apigatewayv2_route" "youtube_get" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "GET ${local.infrastructure.api.callback_paths.youtube}"
  target    = "integrations/${aws_apigatewayv2_integration.youtube.id}"
}
resource "aws_apigatewayv2_route" "youtube_post" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST ${local.infrastructure.api.callback_paths.youtube}"
  target    = "integrations/${aws_apigatewayv2_integration.youtube.id}"
}
resource "aws_apigatewayv2_route" "twitch_post" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST ${local.infrastructure.api.callback_paths.twitch}"
  target    = "integrations/${aws_apigatewayv2_integration.twitch.id}"
}
resource "aws_lambda_permission" "api" {
  for_each      = { youtube = "handle-youtube-notifs", twitch = "handle-twitch-notifs" }
  statement_id  = "AllowApiGateway-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn[each.value].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}
