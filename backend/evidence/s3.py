"""AWS operations only; callers decide authorization and database transitions."""
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings


class StorageUnavailable(Exception):
    pass


class ObjectMissing(Exception):
    pass


def client():
    if not settings.S3_BUCKET_NAME:
        raise StorageUnavailable("S3 is not configured.")
    return boto3.client("s3", region_name=settings.AWS_REGION, config=Config(
        signature_version="s3v4", connect_timeout=2, read_timeout=5,
        retries={"mode": "standard", "total_max_attempts": 2},
    ))


def check_bucket():
    """Deployment probe only; boto3 remains confined to this adapter."""
    try:
        client().head_bucket(Bucket=settings.S3_BUCKET_NAME)
    except (BotoCoreError, ClientError) as exc:
        raise StorageUnavailable("Storage service is unavailable.") from exc


def validate_key(asset):
    parent = f"reports/{asset.report_id}" if asset.report_id else f"incidents/{asset.incident_id}"
    expected = f"evidence/{parent}/{asset.pk.hex}"
    if asset.object_key != expected or not re.fullmatch(r"evidence/(reports|incidents)/[1-9][0-9]*/[a-f0-9]{32}", asset.object_key):
        raise StorageUnavailable("Invalid stored object key.")
    if asset.bucket != settings.S3_BUCKET_NAME:
        raise StorageUnavailable("Unexpected storage bucket.")


def guarded(operation):
    def call(asset, *args, **kwargs):
        validate_key(asset)
        try:
            return operation(asset, *args, **kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise StorageUnavailable("Storage service is unavailable.") from exc
    return call


@guarded
def presign_upload(asset):
    headers = {"Content-Type": asset.content_type, "If-None-Match": "*",
               "x-amz-checksum-sha256": asset.checksum_sha256,
               "x-amz-meta-media-id": str(asset.pk), "x-amz-server-side-encryption": "AES256",
               "x-amz-tagging": "upload-state=pending"}
    url = client().generate_presigned_url("put_object", Params={
        "Bucket": asset.bucket, "Key": asset.object_key, "ContentType": asset.content_type,
        "ContentLength": asset.size_bytes, "IfNoneMatch": "*", "ChecksumSHA256": asset.checksum_sha256,
        "Metadata": {"media-id": str(asset.pk)}, "ServerSideEncryption": "AES256",
        "Tagging": "upload-state=pending",
    }, ExpiresIn=settings.MEDIA_UPLOAD_TTL_SECONDS, HttpMethod="PUT")
    return {"method": "PUT", "url": url, "headers": headers, "content_length": asset.size_bytes}


@guarded
def inspect_object(asset):
    try:
        return client().head_object(Bucket=asset.bucket, Key=asset.object_key, ChecksumMode="ENABLED")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            raise ObjectMissing from exc
        raise


@guarded
def mark_confirmed(asset):
    params = {"Bucket": asset.bucket, "Key": asset.object_key,
              "Tagging": {"TagSet": [{"Key": "upload-state", "Value": "confirmed"}]}}
    if asset.version_id:
        params["VersionId"] = asset.version_id
    client().put_object_tagging(**params)


@guarded
def presign_download(asset):
    params = {"Bucket": asset.bucket, "Key": asset.object_key,
              "ResponseContentDisposition": "attachment", "ResponseContentType": "application/octet-stream"}
    if asset.version_id:
        params["VersionId"] = asset.version_id
    return client().generate_presigned_url("get_object", Params=params, ExpiresIn=settings.MEDIA_DOWNLOAD_TTL_SECONDS)


@guarded
def delete_object(asset):
    # HEAD also discovers the version of an unconfirmed upload, if present.
    try:
        info = inspect_object(asset)
    except ObjectMissing:
        return
    params = {"Bucket": asset.bucket, "Key": asset.object_key}
    if info.get("VersionId"):
        params["VersionId"] = info["VersionId"]
    client().delete_object(**params)
