"""AWS operations only; callers decide authorization and database transitions."""
import re
import base64
import hashlib

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


def optimized_key(asset):
    validate_key(asset)
    expected = asset.object_key + ".optimized"
    if asset.optimized_key and asset.optimized_key != expected:
        raise StorageUnavailable("Invalid derivative key.")
    return expected


@guarded
def read_original(asset):
    from .images import InvalidImage
    params = {"Bucket": asset.bucket, "Key": asset.object_key, "ChecksumMode": "ENABLED"}
    if asset.version_id:
        params["VersionId"] = asset.version_id
    try:
        obj = client().get_object(**params)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NoSuchVersion"):
            raise InvalidImage("original_missing") from exc
        raise
    body = obj["Body"]
    try:
        if (obj.get("ContentLength") != asset.size_bytes or asset.size_bytes > settings.MEDIA_MAX_BYTES
                or obj.get("ContentType") != asset.content_type or obj.get("Metadata", {}).get("media-id") != str(asset.pk)):
            raise InvalidImage("invalid_original")
        data = body.read(asset.size_bytes + 1)
    finally:
        body.close()
    if len(data) != asset.size_bytes or base64.b64encode(hashlib.sha256(data).digest()).decode() != asset.checksum_sha256:
        raise InvalidImage("invalid_original")
    return data


@guarded
def store_optimized(asset, data, content_type):
    """One deterministic conditional object; recover a PUT whose DB update was lost."""
    key = optimized_key(asset)
    checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
    sdk = client()

    def existing():
        try:
            info = sdk.head_object(Bucket=asset.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        if (info.get("ContentLength") != len(data) or info.get("ContentType") != content_type
                or info.get("ChecksumSHA256") != checksum or info.get("Metadata", {}).get("media-id") != str(asset.pk)):
            raise StorageUnavailable("Derivative metadata mismatch.")
        return info

    info = existing()
    if info is None:
        try:
            info = sdk.put_object(Bucket=asset.bucket, Key=key, Body=data, ContentLength=len(data), ContentType=content_type,
                                  ChecksumSHA256=checksum, Metadata={"media-id": str(asset.pk)}, IfNoneMatch="*",
                                  ServerSideEncryption="AES256", Tagging="upload-state=confirmed")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("PreconditionFailed", "412"):
                raise
            info = existing()
            if info is None:
                raise StorageUnavailable("Derivative unavailable.") from exc
    return key, info.get("VersionId", "")


@guarded
def presign_optimized(asset):
    params = {"Bucket": asset.bucket, "Key": optimized_key(asset), "ResponseContentDisposition": "inline",
              "ResponseContentType": asset.optimized_content_type}
    if asset.optimized_version_id:
        params["VersionId"] = asset.optimized_version_id
    return client().generate_presigned_url("get_object", Params=params, ExpiresIn=settings.MEDIA_DOWNLOAD_TTL_SECONDS)


@guarded
def delete_optimized(asset):
    if not asset.optimized_key and not asset.processing_attempts:
        return
    key = optimized_key(asset)
    try:
        info = client().head_object(Bucket=asset.bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return
        raise
    params = {"Bucket": asset.bucket, "Key": key}
    if info.get("VersionId"):
        params["VersionId"] = info["VersionId"]
    client().delete_object(**params)


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
