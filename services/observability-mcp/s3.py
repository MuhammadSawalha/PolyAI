import gzip
import io
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

_BUCKET_BY_ENV = {
    "dev": os.environ.get("DEV_S3_LOGS_BUCKET"),
    "prod": os.environ.get("PROD_S3_LOGS_BUCKET"),
}

s3_client = boto3.client("s3", region_name=AWS_REGION)


def bucket_for(environment: str) -> str:
    bucket = _BUCKET_BY_ENV.get(environment)
    if not bucket:
        raise ValueError(f"Unknown environment '{environment}', expected 'dev' or 'prod'")
    return bucket


def list_log_objects(environment: str, prefix: str) -> list[str]:
    """Lists S3 object keys for the given environment's log bucket under `prefix`."""
    bucket = bucket_for(environment)
    keys: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except ClientError as e:
        logging.error(f"Failed to list objects in s3://{bucket}/{prefix}: {e}")
        raise
    return keys


def download_gzip_json_lines(environment: str, key: str) -> list[dict]:
    """Downloads a gzip'd, newline-delimited-JSON log object and parses it into records."""
    bucket = bucket_for(environment)
    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        logging.error(f"Failed to download s3://{bucket}/{key}: {e}")
        raise
    records = []
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as f:
        for line in f.read().decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logging.warning(f"Skipping unparsable log line in {key}: {line[:200]}")
    return records
