resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${local.prefix}-${each.key}"
  retention_in_days = 14
}

resource "aws_lambda_function" "fn" {
  for_each         = local.functions
  function_name    = "${local.prefix}-${each.key}"
  role             = aws_iam_role.lambda[each.key].arn
  runtime          = "python3.12"
  handler          = "${replace(each.key, "-", "_")}.handler"
  filename         = "${var.lambda_artifact_directory}/${each.key}.zip"
  source_code_hash = filebase64sha256("${var.lambda_artifact_directory}/${each.key}.zip")
  architectures    = ["arm64"]
  timeout          = 120
  memory_size      = each.key == "get-stream-stats" ? 512 : 256
  environment {
    variables = {
      EVENT_BUS_NAME                 = aws_cloudwatch_event_bus.stream_events.name
      DATABASE_HOST                  = aws_db_instance.postgres.address
      DATABASE_PORT                  = tostring(aws_db_instance.postgres.port)
      DATABASE_NAME                  = aws_db_instance.postgres.db_name
      DATABASE_SECRET_ARN            = aws_db_instance.postgres.master_user_secret[0].secret_arn
      DATABASE_SSLMODE               = "require"
      YOUTUBE_SECRET_PARAMETER       = var.youtube_websub_secret_parameter
      TWITCH_SECRET_PARAMETER        = var.twitch_secret_parameter
      TWITCH_CLIENT_ID_PARAMETER     = var.twitch_client_id_parameter
      TWITCH_CLIENT_SECRET_PARAMETER = var.twitch_client_secret_parameter
    }
  }
  vpc_config {
    security_group_ids = [aws_security_group.lambda.id]
    subnet_ids         = values(aws_subnet.private)[*].id
  }
  depends_on = [aws_iam_role_policy_attachment.lambda_logs, aws_iam_role_policy_attachment.lambda_vpc, aws_cloudwatch_log_group.fn, aws_route_table_association.private]
}
