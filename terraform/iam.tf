data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  prefix    = "${local.settings.project}-${local.settings.environment}"
  functions = toset(keys(local.build_config.functions))
  function_parameters = {
    subscribe-to-websub   = [local.settings.youtube_websub_secret_parameter]
    subscribe-to-eventsub = [local.settings.twitch_secret_parameter, local.settings.twitch_client_id_parameter, local.settings.twitch_client_secret_parameter]
    handle-youtube-notifs = [local.settings.youtube_websub_secret_parameter]
    handle-twitch-notifs  = [local.settings.twitch_secret_parameter]
    handle-stream-events  = [local.settings.youtube_api_key_parameter, local.settings.twitch_client_id_parameter, local.settings.twitch_client_secret_parameter]
    get-stream-stats      = [local.settings.youtube_api_key_parameter, local.settings.twitch_client_id_parameter, local.settings.twitch_client_secret_parameter]
    update-channels       = [local.settings.youtube_api_key_parameter, local.settings.twitch_client_id_parameter, local.settings.twitch_client_secret_parameter]
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each           = local.functions
  name               = "${local.prefix}-${each.key}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  for_each   = local.functions
  role       = aws_iam_role.lambda[each.key].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  for_each   = local.functions
  role       = aws_iam_role.lambda[each.key].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "function_permissions" {
  for_each = local.functions
  dynamic "statement" {
    for_each = contains(local.infrastructure.lambda.event_publishers, each.key) ? [1] : []
    content {
      actions   = ["events:PutEvents"]
      resources = [aws_cloudwatch_event_bus.stream_events.arn]
    }
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.postgres.master_user_secret[0].secret_arn]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [for p in local.function_parameters[each.key] : "arn:${data.aws_partition.current.partition}:ssm:${local.settings.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(p, "/")}"]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.ingestion_failures.arn]
  }
}

resource "aws_iam_role_policy" "function" {
  for_each = local.functions
  name     = "${local.prefix}-${each.key}-permissions"
  role     = aws_iam_role.lambda[each.key].id
  policy   = data.aws_iam_policy_document.function_permissions[each.key].json
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "scheduler" {
  name               = "${local.prefix}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}
resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-scheduled-lambdas"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = [
      for job in values(local.schedules) : aws_lambda_function.fn[job.lambda].arn
    ] }, { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.ingestion_failures.arn }]
  })
}
