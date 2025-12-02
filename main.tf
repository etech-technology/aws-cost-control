terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# Package the lambda code
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda/lambda_function.py"
  output_path = "${path.module}/lambda/lambda.zip"
}

# IAM role for Lambda
resource "aws_iam_role" "lambda_role" {
  name               = "${var.lambda_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    actions = ["sts:AssumeRole"]
  }
}

# Inline policy for EC2 + IAM key management + Secrets Manager + logs
data "aws_iam_policy_document" "lambda_policy" {
  statement {
    sid    = "Ec2Control"
    effect = "Allow"

    actions = [
      "ec2:DescribeInstances",
      "ec2:StopInstances",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "IamKeyManagement"
    effect = "Allow"

    actions = [
      "iam:ListUsers",
      "iam:ListAccessKeys",
      "iam:GetAccessKeyLastUsed",
      "iam:UpdateAccessKey",
      "iam:CreateAccessKey"
    ]

    resources = ["*"]
  }

  # Secrets Manager: create/update per-user access key secrets
  statement {
    sid    = "SecretsManagerAccess"
    effect = "Allow"

    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:PutSecretValue",
      "secretsmanager:DescribeSecret",
      "secretsmanager:ListSecrets"
    ]

    resources = ["*"]
  }

  statement {
    sid    = "Logs"
    effect = "Allow"

    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]

    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda_inline" {
  name   = "${var.lambda_name}-policy"
  role   = aws_iam_role.lambda_role.id
  policy = data.aws_iam_policy_document.lambda_policy.json
}

# Lambda function
resource "aws_lambda_function" "this" {
  function_name = var.lambda_name
  role          = aws_iam_role.lambda_role.arn
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.12"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout = 900 # 15 minutes

  environment {
    variables = {
      DRY_RUN              = var.dry_run ? "true" : "false"
      EC2_FILTER_TAG_KEY   = var.ec2_filter_tag_key
      EC2_FILTER_TAG_VALUE = var.ec2_filter_tag_value
      IAM_ALLOWED_USERS    = join(",", var.iam_allowed_users)
      SECRET_NAME_PREFIX   = var.secret_name_prefix
      SLACK_WEBHOOK_URL    = var.slack_webhook_url
    }
  }
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler – 1am America/New_York, timezone-aware
# ---------------------------------------------------------------------------

# Execution role for the Scheduler – lets scheduler invoke the Lambda
resource "aws_iam_role" "scheduler_lambda" {
  name = "${var.lambda_name}-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "scheduler.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

data "aws_iam_policy_document" "scheduler_lambda" {
  statement {
    effect = "Allow"

    actions = [
      "lambda:InvokeFunction"
    ]

    resources = [
      aws_lambda_function.this.arn
    ]
  }
}

resource "aws_iam_role_policy" "scheduler_lambda" {
  name   = "${var.lambda_name}-scheduler-policy"
  role   = aws_iam_role.scheduler_lambda.id
  policy = data.aws_iam_policy_document.scheduler_lambda.json
}

# EventBridge Scheduler schedule
# Cron format: minutes hours day-of-month month day-of-week year
# 1:00 every day, evaluated in America/New_York timezone (DST-aware).
resource "aws_scheduler_schedule" "daily_1am_est" {
  name        = "${var.lambda_name}-schedule"
  description = "Run ${var.lambda_name} daily at 1am America/New_York."

  schedule_expression          = "cron(0 1 * * ? *)"
  schedule_expression_timezone = "America/New_York"

  state = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.this.arn
    role_arn = aws_iam_role.scheduler_lambda.arn

    # Empty JSON event – handler doesn't need any event data
    input = jsonencode({})
  }
}
