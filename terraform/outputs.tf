output "youtube_webhook_url" {
  description = "Public YouTube WebSub callback URL."
  value       = "${aws_apigatewayv2_stage.api.invoke_url}youtube/webhook"
}

output "twitch_webhook_url" {
  description = "Public Twitch EventSub callback URL."
  value       = "${aws_apigatewayv2_stage.api.invoke_url}twitch/webhook"
}

output "postgres_endpoint" {
  description = "Private PostgreSQL hostname and port."
  value       = aws_db_instance.postgres.endpoint
}

output "postgres_database_name" {
  description = "PostgreSQL database containing the streamer tracker schema."
  value       = aws_db_instance.postgres.db_name
}

output "postgres_master_secret_arn" {
  description = "Secrets Manager secret containing the RDS master credentials."
  value       = aws_db_instance.postgres.master_user_secret[0].secret_arn
}
output "event_bus_name" { value = aws_cloudwatch_event_bus.stream_events.name }
output "lambda_function_names" { value = { for key, fn in aws_lambda_function.fn : key => fn.function_name } }
