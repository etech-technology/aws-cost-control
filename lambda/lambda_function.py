import boto3
from datetime import datetime, timezone, timedelta
import os
import json
import urllib.request
import urllib.error

ec2 = boto3.client("ec2")
iam = boto3.client("iam")
secretsmanager = boto3.client("secretsmanager")

# DRY_RUN: "true" → log only, no changes. Set to "false" in env to enable.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Optional EC2 tag filter (for safety)
EC2_FILTER_TAG_KEY = os.getenv("EC2_FILTER_TAG_KEY")       # e.g. "AutoStop"
EC2_FILTER_TAG_VALUE = os.getenv("EC2_FILTER_TAG_VALUE")   # e.g. "true"

# Optional: restrict IAM key management to specific users
IAM_ALLOWED_USERS = os.getenv("IAM_ALLOWED_USERS", "")
IAM_ALLOWED_USERS = {u.strip() for u in IAM_ALLOWED_USERS.split(",") if u.strip()}

# Where to store IAM user access keys in Secrets Manager
# Secret name pattern: <SECRET_NAME_PREFIX><username>/access-key
SECRET_NAME_PREFIX = os.getenv("SECRET_NAME_PREFIX", "iam/user/")

# Slack webhook URL (Incoming Webhook)
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    print(f"Lambda started at {now.isoformat()}, DRY_RUN={DRY_RUN}")

    ec2_stats = stop_old_ec2_instances(now)
    iam_stats = manage_iam_keys(now)

    print("Lambda run complete.")

    # Build and send Slack summary
    summary = build_summary(now, ec2_stats, iam_stats)
    print("Summary:\n" + summary)
    send_slack_notification(summary)

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 1) EC2: Stop instances running > 24 hours
# ---------------------------------------------------------------------------

def stop_old_ec2_instances(now):
    print("Checking for EC2 instances running > 24 hours...")

    filters = [
        {"Name": "instance-state-name", "Values": ["running"]}
    ]

    # Optional tag filter for safety
    if EC2_FILTER_TAG_KEY and EC2_FILTER_TAG_VALUE:
        filters.append({
            "Name": f"tag:{EC2_FILTER_TAG_KEY}",
            "Values": [EC2_FILTER_TAG_VALUE]
        })

    paginator = ec2.get_paginator("describe_instances")
    page_iterator = paginator.paginate(Filters=filters)

    instances_to_stop = []

    for page in page_iterator:
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                launch_time = instance["LaunchTime"]  # tz-aware UTC
                running_duration = now - launch_time

                print(f"Instance {instance_id} launched at {launch_time}, "
                      f"running for {running_duration}")

                if running_duration > timedelta(hours=24):
                    instances_to_stop.append(instance_id)

    if not instances_to_stop:
        print("No instances to stop.")
        return {
            "instances_to_stop": 0,
            "instances_stopped": 0,
        }

    print(f"Instances to stop (>24h): {instances_to_stop}")

    instances_stopped = 0

    if DRY_RUN:
        print("DRY_RUN enabled: not calling StopInstances.")
    else:
        try:
            response = ec2.stop_instances(InstanceIds=instances_to_stop)
            print("StopInstances response:", response)
            instances_stopped = len(instances_to_stop)
        except Exception as e:
            print(f"Error stopping instances: {e}")

    return {
        "instances_to_stop": len(instances_to_stop),
        "instances_stopped": instances_stopped,
    }


# ---------------------------------------------------------------------------
# 2 & 3) IAM: Deactivate keys inactive >60 days, rotate keys >30 days
#     + store rotated keys in Secrets Manager
# ---------------------------------------------------------------------------

def manage_iam_keys(now):
    print("Checking IAM access keys...")

    inactive_threshold = timedelta(days=60)
    rotate_threshold = timedelta(days=30)

    paginator = iam.get_paginator("list_users")

    users_processed = 0
    keys_deactivated = 0
    keys_rotated = 0

    for page in paginator.paginate():
        for user in page.get("Users", []):
            username = user["UserName"]

            if IAM_ALLOWED_USERS and username not in IAM_ALLOWED_USERS:
                print(f"Skipping user {username} (not in IAM_ALLOWED_USERS).")
                continue

            print(f"Processing user: {username}")
            users_processed += 1
            d, r = process_user_keys(username, now, inactive_threshold, rotate_threshold)
            keys_deactivated += d
            keys_rotated += r

    return {
        "users_processed": users_processed,
        "keys_deactivated": keys_deactivated,
        "keys_rotated": keys_rotated,
    }


def process_user_keys(username, now, inactive_threshold, rotate_threshold):
    access_keys = iam.list_access_keys(UserName=username)["AccessKeyMetadata"]

    if not access_keys:
        print(f"User {username} has no access keys.")
        return 0, 0

    created_new_key = None  # ensure max one new key per user per run

    deactivated_count = 0
    rotated_count = 0

    for key_meta in access_keys:
        access_key_id = key_meta["AccessKeyId"]
        status = key_meta["Status"]
        create_date = key_meta["CreateDate"]  # datetime
        key_age = now - create_date

        print(f"  Key {access_key_id}: status={status}, create_date={create_date}, age={key_age}")

        # Inactivity check (>60 days)
        last_used_resp = iam.get_access_key_last_used(AccessKeyId=access_key_id)
        last_used = last_used_resp.get("AccessKeyLastUsed", {}).get("LastUsedDate")

        if last_used:
            inactivity_age = now - last_used
            print(f"    Last used at {last_used}, inactivity_age={inactivity_age}")
        else:
            inactivity_age = now - create_date
            print(f"    Never used, treating inactivity_age={inactivity_age}")

        # Deactivate keys inactive >60 days
        if inactivity_age > inactive_threshold:
            print(f"    Key {access_key_id} inactive >60 days → will deactivate.")
            if not DRY_RUN:
                deactivate_key(username, access_key_id)
            else:
                print(f"    DRY_RUN: would deactivate inactive key {access_key_id}.")
            deactivated_count += 1
            # no rotation for inactive keys
            continue

        # Rotate keys older than 30 days (still Active)
        if status == "Active" and key_age > rotate_threshold:
            print(f"    Key {access_key_id} is active and older than 30 days → rotation needed")

            if created_new_key is None:
                if not DRY_RUN:
                    created_new_key = create_new_access_key(username)
                else:
                    print("    DRY_RUN: would create a new access key for this user.")
                    created_new_key = "DRY_RUN_PLACEHOLDER"

            if not DRY_RUN:
                deactivate_key(username, access_key_id)
            else:
                print(f"    DRY_RUN: would deactivate old key {access_key_id} after rotation.")
            rotated_count += 1

    return deactivated_count, rotated_count


def deactivate_key(username, access_key_id):
    try:
        print(f"    Deactivating key {access_key_id} for user {username}...")
        iam.update_access_key(
            UserName=username,
            AccessKeyId=access_key_id,
            Status="Inactive"
        )
        print(f"    Key {access_key_id} deactivated.")
    except Exception as e:
        print(f"    Error deactivating key {access_key_id} for user {username}: {e}")


def create_new_access_key(username):
    try:
        print(f"    Creating new access key for user {username}...")
        resp = iam.create_access_key(UserName=username)
        access_key = resp["AccessKey"]
        new_key_id = access_key["AccessKeyId"]

        print(f"    Created new key {new_key_id} for user {username}.")

        # Store in Secrets Manager (no secret values in logs)
        store_access_key_in_secrets_manager(username, access_key)

        return access_key
    except Exception as e:
        print(f"    Error creating new access key for user {username}: {e}")
        return None


def store_access_key_in_secrets_manager(username, access_key):
    """
    Store access key in AWS Secrets Manager.

    Secret name: <SECRET_NAME_PREFIX><username>/access-key
    Secret value: JSON with AccessKeyId, SecretAccessKey, CreateDate
    """
    secret_name = f"{SECRET_NAME_PREFIX}{username}/access-key"

    # Convert CreateDate to ISO string if present
    create_date = access_key.get("CreateDate")
    if isinstance(create_date, datetime):
        create_date_str = create_date.isoformat()
    else:
        create_date_str = str(create_date) if create_date else None

    payload = {
        "UserName": username,
        "AccessKeyId": access_key["AccessKeyId"],
        "SecretAccessKey": access_key["SecretAccessKey"],  # DO NOT log this
        "CreateDate": create_date_str,
    }

    secret_string = json.dumps(payload)

    try:
        # First try to create the secret
        print(f"    Storing new access key for {username} in Secrets Manager secret '{secret_name}'...")
        secretsmanager.create_secret(
            Name=secret_name,
            SecretString=secret_string,
        )
        print(f"    Created new secret '{secret_name}'.")
    except secretsmanager.exceptions.ResourceExistsException:
        # Secret already exists → write new version
        try:
            print(f"    Secret '{secret_name}' already exists, writing new version...")
            secretsmanager.put_secret_value(
                SecretId=secret_name,
                SecretString=secret_string,
            )
            print(f"    Updated secret '{secret_name}' with new key version.")
        except Exception as e:
            print(f"    Error updating secret '{secret_name}': {e}")
    except Exception as e:
        print(f"    Error creating secret '{secret_name}': {e}")


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def build_summary(now, ec2_stats, iam_stats):
    lines = [
        f"*Cost Guardian Lambda run*",
        f"Time (UTC): `{now.isoformat()}`",
        f"DRY_RUN: `{DRY_RUN}`",
        "",
        f"*EC2*",
        f"- Candidates to stop (>24h): `{ec2_stats['instances_to_stop']}`",
        f"- Actually stopped: `{ec2_stats['instances_stopped']}`",
        "",
        f"*IAM Access Keys*",
        f"- Users processed: `{iam_stats['users_processed']}`",
        f"- Keys deactivated (>60d inactive): `{iam_stats['keys_deactivated']}`",
        f"- Keys rotated (>30d age): `{iam_stats['keys_rotated']}`",
    ]
    # Use Slack's basic markdown
    return "\n".join(lines)


def send_slack_notification(text):
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set; skipping Slack notification.")
        return

    payload = {"text": text}

    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            print(f"Slack response status={resp.status}, body={body}")
    except urllib.error.HTTPError as e:
        print(f"HTTP error sending Slack notification: {e.code} {e.reason}")
        try:
            print(e.read().decode("utf-8"))
        except Exception:
            pass
    except Exception as e:
        print(f"Error sending Slack notification: {e}")
