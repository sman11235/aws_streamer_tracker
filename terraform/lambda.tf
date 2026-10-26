resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${local.prefix}-${each.key}"
  retention_in_days = local.infrastructure.lambda.log_retention_days
}

resource "aws_lambda_function" "fn" {
  for_each         = local.functions
  function_name    = "${local.prefix}-${each.key}"
  role             = aws_iam_role.lambda[each.key].arn
  runtime          = local.infrastructure.lambda.runtime
  handler          = "${replace(each.key, "-", "_")}.handler"
  filename         = "${local.settings.lambda_artifact_directory}/${each.key}.zip"
  source_code_hash = filebase64sha256("${local.settings.lambda_artifact_directory}/${each.key}.zip")
  architectures    = local.infrastructure.lambda.architectures
  timeout          = local.infrastructure.lambda.timeout_seconds
  memory_size      = lookup(local.infrastructure.lambda.memory_overrides_mb, each.key, local.infrastructure.lambda.memory_mb)
  environment {
    variables = {
      (local.runtime_config.environment.event_bus_name)       = aws_cloudwatch_event_bus.stream_events.name
      (local.runtime_config.environment.database_host)        = aws_db_instance.postgres.address
      (local.runtime_config.environment.database_port)        = tostring(aws_db_instance.postgres.port)
      (local.runtime_config.environment.database_name)        = aws_db_instance.postgres.db_name
      (local.runtime_config.environment.database_secret_arn)  = aws_db_instance.postgres.master_user_secret[0].secret_arn
      (local.runtime_config.environment.youtube_secret)       = local.settings.youtube_websub_secret_parameter
      (local.runtime_config.environment.twitch_secret)        = local.settings.twitch_secret_parameter
      (local.runtime_config.environment.twitch_client_id)     = local.settings.twitch_client_id_parameter
      (local.runtime_config.environment.twitch_client_secret) = local.settings.twitch_client_secret_parameter
      (local.runtime_config.environment.youtube_api_key)      = local.settings.youtube_api_key_parameter
      (local.runtime_config.environment.youtube_callback)     = "${aws_apigatewayv2_api.api.api_endpoint}${local.infrastructure.api.callback_paths.youtube}"
      (local.runtime_config.environment.twitch_callback)      = "${aws_apigatewayv2_api.api.api_endpoint}${local.infrastructure.api.callback_paths.twitch}"
    }
  }
  vpc_config {
    security_group_ids = [aws_security_group.lambda.id]
    subnet_ids         = values(aws_subnet.private)[*].id
  }
  depends_on = [aws_iam_role_policy_attachment.lambda_logs, aws_iam_role_policy_attachment.lambda_vpc, aws_iam_role_policy.function, aws_cloudwatch_log_group.fn, aws_route_table_association.private]
}
