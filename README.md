# AWS Cost Guardian Lambda

This repo deploys a scheduled AWS Lambda function that:

1. **Runs every day at 1:00 AM America/New_York** (DST-aware) using **EventBridge Scheduler**.
2. **Stops EC2 instances** that have been running for **more than 24 hours** (optionally filtered by tag).
3. **Manages IAM access keys**:
   - Deactivates keys that have been **inactive for > 60 days**.
   - Rotates keys that are **older than 30 days**.
4. **Stores rotated keys** in **AWS Secrets Manager** per IAM user.
5. Sends a **Slack notification** summarizing what it did.

It’s designed for teaching and as a starting point for cost/security automation.

---

## Architecture

- **Lambda**
  - Python 3.12
  - Uses `boto3` (available by default in the Lambda runtime)
  - Environment controls:
    - `DRY_RUN` – log only, no changes if `true`.
    - `EC2_FILTER_TAG_KEY` / `EC2_FILTER_TAG_VALUE` – restrict which EC2 instances are auto-stopped.
    - `IAM_ALLOWED_USERS` – restrict which IAM users are managed.
    - `SECRET_NAME_PREFIX` – prefix for per-user Secrets Manager secrets.
    - `SLACK_WEBHOOK_URL` – Slack Incoming Webhook for notifications.
- **EventBridge Scheduler**
  - `cron(0 1 * * ? *)`
  - `schedule_expression_timezone = "America/New_York"`
  - Invokes the Lambda via a dedicated scheduler execution role.
- **Secrets Manager**
  - One secret per user:
    - Name: `<SECRET_NAME_PREFIX><username>/access-key`
    - Value: JSON with `UserName`, `AccessKeyId`, `SecretAccessKey`, `CreateDate`.

---

## Prerequisites

- Terraform `>= 1.5`
- AWS account + IAM user/role with permission to create:
  - Lambda functions
  - IAM roles/policies
  - EventBridge Scheduler schedules
  - Secrets Manager secrets
- Optional: Slack Incoming Webhook URL.

---

## Usage

### 1. Clone / init

```bash
git clone <your-fork-or-repo-url> aws-cost-guardian
cd aws-cost-guardian

terraform init
```

### 2. Configure variables

You can use the defaults, or override with a `.tfvars` file:

**`dev.auto.tfvars` (example):**

```hcl
region           = "us-east-1"
lambda_name      = "cost-guardian-lambda"
dry_run          = true

# Only stop instances tagged AutoStop = true
ec2_filter_tag_key   = "AutoStop"
ec2_filter_tag_value = "true"

# Only manage certain users (optional)
iam_allowed_users = ["dev-user1", "dev-user2"]

# Secrets prefix
secret_name_prefix = "iam/user/"

# Slack Webhook (from Slack Incoming Webhooks)
slack_webhook_url = "https://hooks.slack.com/services/XXX/YYY/ZZZ"
```

> **Tip:** Start with `dry_run = true` so it only logs what it *would* do.

### 3. Apply

```bash
terraform plan
terraform apply
```

The Lambda will be packaged from `lambda/lambda_function.py` into a ZIP and deployed.

### 4. Check CloudWatch Logs + Slack

- Invoke the Lambda manually once from the console to test.
- Confirm:
  - Logs show which EC2 instances & IAM keys it considered.
  - Slack receives a summary message.

### 5. Turn off DRY RUN

When you are comfortable with what it’s doing:

```bash
terraform apply -var 'dry_run=false'
```

---

---

## Clean up

To remove all resources:

```bash
terraform destroy
```
