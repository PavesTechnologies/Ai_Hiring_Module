import boto3
from botocore.exceptions import ClientError

from app.core.config import settings


def _client():
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )


def upload_file(file_bytes: bytes, key: str, content_type: str = "application/octet-stream") -> str:
    """Upload bytes to S3; returns the object key."""
    client = _client()
    client.put_object(
        Bucket=settings.aws_s3_bucket,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
    )
    return key


def generate_presigned_url(key: str, expires_in: int = 3600) -> str:
    """Return a pre-signed GET URL valid for *expires_in* seconds."""
    client = _client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.aws_s3_bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def delete_file(key: str) -> None:
    client = _client()
    client.delete_object(Bucket=settings.aws_s3_bucket, Key=key)


def file_exists(key: str) -> bool:
    try:
        _client().head_object(Bucket=settings.aws_s3_bucket, Key=key)
        return True
    except ClientError:
        return False
