# s3.py
import os
import boto3
from botocore.exceptions import ClientError
import logging

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET")

if not AWS_S3_BUCKET:
    logging.warning("⚠️ AWS_S3_BUCKET environment variable is not set!")

# Initialize S3 Client
s3_client = boto3.client("s3", region_name=AWS_REGION)

def upload_file_bytes(file_bytes: bytes, s3_key: str, content_type: str = "image/jpeg") -> bool:
    """Uploads raw bytes directly to an S3 object key."""
    try:
        s3_client.put_object(
            Bucket=AWS_S3_BUCKET,
            Key=s3_key,
            Body=file_bytes,
            ContentType=content_type
        )
        logging.info(f"Successfully uploaded to S3: {s3_key}")
        return True
    except ClientError as e:
        logging.error(f"Failed to upload to S3 ({s3_key}): {e}")
        return False

def upload_local_file(local_path: str, s3_key: str) -> bool:
    """Uploads a local file from disk to an S3 object key."""
    try:
        s3_client.upload_file(local_path, AWS_S3_BUCKET, s3_key)
        logging.info(f"Successfully uploaded local file {local_path} to S3: {s3_key}")
        return True
    except ClientError as e:
        logging.error(f"Failed to upload local file to S3: {e}")
        return False

def download_file_bytes(s3_key: str) -> bytes:
    """Downloads an S3 object directly into memory bytes."""
    try:
        response = s3_client.get_object(Bucket=AWS_S3_BUCKET, Key=s3_key)
        return response["Body"].read()
    except ClientError as e:
        logging.error(f"Failed to download from S3 ({s3_key}): {e}")
        raise e