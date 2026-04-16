"""
Kinetic Revenue Agent — Shared Credentials Module

Single source of truth for loading credentials from AWS Secrets Manager
or .env. Every script in the pipeline imports from here.

Usage in scripts:
    from credentials import load_credentials, add_credentials_args

    parser = argparse.ArgumentParser(...)
    add_credentials_args(parser)
    args = parser.parse_args()
    load_credentials(secret_name=args.secrets, region=args.region)
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()

SECRET_NAME = "kinetic-revenue-agent"
SECRET_REGION = "us-east-2"


def load_from_secrets_manager(secret_name=SECRET_NAME, region=SECRET_REGION):
    """Load credentials from AWS Secrets Manager (production on EC2)."""
    import boto3
    sm = boto3.client("secretsmanager", region_name=region)
    response = sm.get_secret_value(SecretId=secret_name)
    secrets = json.loads(response["SecretString"])
    for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD",
                "FMP_API_KEY", "ANTHROPIC_API_KEY", "RAPIDAPI_KEY",
                "SLACK_WEBHOOK", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID",
                "X_BEARER_TOKEN",
                "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                "EMAIL_FROM", "EMAIL_TO"):
        if key in secrets:
            os.environ[key] = secrets[key]
    print(f"[credentials] Loaded from Secrets Manager: {secret_name} ({region})")


def load_credentials(secret_name=None, region=SECRET_REGION):
    """Load credentials from Secrets Manager or .env.

    Priority:
      1. Explicit --secrets flag (always uses Secrets Manager)
      2. ENVIRONMENT != "local" (auto-detect EC2, uses Secrets Manager)
      3. Fall back to .env (local development)
    """
    if secret_name:
        load_from_secrets_manager(secret_name, region)
    elif os.getenv("ENVIRONMENT", "local") != "local":
        load_from_secrets_manager(SECRET_NAME, region)
    else:
        pass  # .env already loaded by load_dotenv() at import time


def add_credentials_args(parser):
    """Add --secrets and --region arguments to an argparse parser."""
    parser.add_argument("--secrets", type=str, default=None,
                        help="AWS Secrets Manager secret name (omit for .env)")
    parser.add_argument("--region", type=str, default="us-east-2",
                        help="AWS region (default: us-east-2)")
