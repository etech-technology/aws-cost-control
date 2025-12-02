variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "lambda_name" {
  description = "Name of the Lambda function"
  type        = string
  default     = "cost-guardian-lambda"
}

variable "dry_run" {
  description = "If true, Lambda only logs actions (no changes)."
  type        = bool
  default     = true
}

variable "ec2_filter_tag_key" {
  description = "Optional tag key used to select EC2 instances to manage (e.g. AutoStop)"
  type        = string
  default     = ""
}

variable "ec2_filter_tag_value" {
  description = "Optional tag value used to select EC2 instances to manage (e.g. true)"
  type        = string
  default     = ""
}

variable "iam_allowed_users" {
  description = "Optional list of IAM usernames whose keys should be managed. Empty = all users."
  type        = list(string)
  default     = []
}

variable "secret_name_prefix" {
  description = "Prefix for Secrets Manager secret names for IAM access keys (e.g. 'iam/user/')."
  type        = string
  default     = "iam/user/"
}

variable "slack_webhook_url" {
  description = "Slack Incoming Webhook URL for notifications. Leave empty to disable."
  type        = string
  default     = ""
}
