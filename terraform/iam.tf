data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  prefix = "${var.project}-${var.environment}"
  functions = toset([
    "subscribe-to-websub", "subscribe-to-eventsub", "handle-youtube-notifs",
    "handle-twitch-notifs", "handle-stream-events", "get-stream-stats"
  ])
  secret_parameters = [
    var.youtube_websub_secret_parameter, var.twitch_secret_parameter,
    var.twitch_client_id_parameter, var.twitch_client_secret_parameter
  ]
  secret_arns = [for p in local.secret_parameters : "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(p, "/")}"]
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
    for_each = contains(["handle-youtube-notifs", "handle-twitch-notifs"], each.key) ? [1] : []
    content {
      actions   = ["events:PutEvents"]
      resources = [aws_cloudwatch_event_bus.stream_events.arn]
    }
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.postgres.master_user_secret[0].secret_arn]
  }
  dynamic "statement" {
    for_each = contains(["subscribe-to-eventsub", "handle-twitch-notifs", "get-stream-stats", "handle-youtube-notifs", "subscribe-to-websub"], each.key) ? [1] : []
    content {
      actions   = ["ssm:GetParameter"]
      resources = local.secret_arns
    }
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
      aws_lambda_function.fn["subscribe-to-websub"].arn,
      aws_lambda_function.fn["subscribe-to-eventsub"].arn,
      aws_lambda_function.fn["get-stream-stats"].arn
    ] }]
  })
}
