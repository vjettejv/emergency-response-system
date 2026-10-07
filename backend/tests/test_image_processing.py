import base64
import hashlib
import io
import random
import time
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
from celery.contrib.testing.worker import start_worker
from django.conf import settings
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from config.celery import app
from evidence import s3
from evidence.images import InvalidImage, optimize_image
from evidence.models import MediaAsset
from evidence.services import confirm_upload, delete_media, download_url
from evidence.tasks import cleanup_media, optimize_media, sweep_images
from tests.test_media_tasks import fixture


def image_bytes(kind="JPEG", size=(1200, 800), noise=False):
    data = random.Random(85).randbytes(size[0] * size[1] * 3) if noise else bytes([40, 100, 160]) * (size[0] * size[1])
    image = Image.frombytes("RGB", size, data)
    output = io.BytesIO(); image.save(output, format=kind, quality=95)
    return output.getvalue()


def asset_fixture(data=None, mime="image/jpeg"):
    data = image_bytes() if data is None else data
    asset = fixture()
    asset.status, asset.confirmed_at = "ready", timezone.now()
    asset.bucket = settings.S3_BUCKET_NAME
    asset.object_key = f"evidence/reports/{asset.report_id}/{asset.pk.hex}"
    asset.content_type, asset.size_bytes = mime, len(data)
    asset.checksum_sha256 = base64.b64encode(hashlib.sha256(data).digest()).decode()
    asset.processing_status, asset.next_processing_at = "pending", timezone.now() - timedelta(seconds=1)
    asset.save()
    return asset, data


@override_settings(S3_BUCKET_NAME="synthetic-private-bucket", MEDIA_IMAGE_MAX_DIMENSION=600)
class ImageProcessingTests(TestCase):
    def sdk(self):
        return boto3.client("s3", region_name="ap-southeast-1", aws_access_key_id="synthetic", aws_secret_access_key="synthetic")

    def test_real_jpeg_png_webp_decode_resize_and_exif_rotation(self):
        for kind, mime in [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")]:
            output, content_type, width, height = optimize_image(image_bytes(kind), mime)
            self.assertEqual((width, height), (600, 400)); self.assertEqual(content_type, "image/jpeg")
            with Image.open(io.BytesIO(output)) as decoded:
                self.assertEqual(decoded.format, "JPEG"); self.assertFalse(decoded.getexif())
        original = Image.new("RGB", (800, 400)); exif = Image.Exif(); exif[274] = 6
        raw = io.BytesIO(); original.save(raw, format="JPEG", exif=exif)
        _, _, width, height = optimize_image(raw.getvalue(), "image/jpeg")
        self.assertEqual((width, height), (300, 600))
        with override_settings(MEDIA_IMAGE_OUTPUT_FORMAT="WEBP"):
            output, mime, _, _ = optimize_image(image_bytes("PNG"), "image/png")
            self.assertEqual(mime, "image/webp")

    def test_size_pixel_mime_and_corrupt_limits(self):
        for data, mime in [(b"broken", "image/jpeg"), (image_bytes("PNG"), "image/jpeg"), (b"video", "video/mp4")]:
            with self.assertRaises(InvalidImage): optimize_image(data, mime)
        with override_settings(MEDIA_IMAGE_MAX_PIXELS=100):
            with self.assertRaises(InvalidImage): optimize_image(image_bytes(), "image/jpeg")
        with override_settings(MEDIA_MAX_BYTES=10):
            with self.assertRaises(InvalidImage): optimize_image(image_bytes(), "image/jpeg")

    def test_s3_read_is_bounded_checks_bytes_and_closes_body(self):
        asset, data = asset_fixture(); sdk = self.sdk()
        for raw in [data, data[:-1] + bytes([data[-1] ^ 1])]:
            body = StreamingBody(io.BytesIO(raw), len(raw))
            with patch("evidence.s3.client", return_value=sdk), Stubber(sdk) as stub:
                stub.add_response("get_object", {"Body": body, "ContentLength": len(raw), "ContentType": asset.content_type,
                                                  "Metadata": {"media-id": str(asset.pk)}},
                                  {"Bucket": asset.bucket, "Key": asset.object_key, "ChecksumMode": "ENABLED"})
                if raw == data: self.assertEqual(s3.read_original(asset), data)
                else:
                    with self.assertRaises(InvalidImage): s3.read_original(asset)
                self.assertTrue(body._raw_stream.closed)

    def test_task_uploads_one_private_optimized_object_and_preserves_original(self):
        asset, data = asset_fixture(); sdk = self.sdk()
        optimized, mime, width, height = optimize_image(data, asset.content_type)
        key = asset.object_key + ".optimized"
        checksum = base64.b64encode(hashlib.sha256(optimized).digest()).decode()
        with patch("evidence.tasks.s3.read_original", return_value=data) as original, patch("evidence.s3.client", return_value=sdk), Stubber(sdk) as stub:
            stub.add_client_error("head_object", service_error_code="404", http_status_code=404,
                                  expected_params={"Bucket": asset.bucket, "Key": key, "ChecksumMode": "ENABLED"})
            stub.add_response("put_object", {"VersionId": "optimized-v1"}, {"Bucket": asset.bucket, "Key": key, "Body": optimized,
                              "ContentLength": len(optimized), "ContentType": mime, "ChecksumSHA256": checksum,
                              "Metadata": {"media-id": str(asset.pk)}, "IfNoneMatch": "*", "ServerSideEncryption": "AES256", "Tagging": "upload-state=confirmed"})
            self.assertEqual(optimize_media(str(asset.pk)), "ready")
            self.assertEqual(optimize_media(str(asset.pk)), "skipped")
            original.assert_called_once(); stub.assert_no_pending_responses()
        asset.refresh_from_db()
        self.assertEqual(asset.object_key, key.removesuffix(".optimized"))
        self.assertEqual(asset.size_bytes, len(data)); self.assertEqual(asset.status, "ready")
        self.assertEqual(asset.optimized_size, len(optimized)); self.assertEqual((asset.width, asset.height), (width, height))
        self.assertEqual(asset.processing_attempts, 1)

    def test_retry_reuses_existing_derivative_after_db_update_was_lost(self):
        asset, data = asset_fixture(); sdk = self.sdk(); output, mime, _, _ = optimize_image(data, asset.content_type)
        info = {"ContentLength": len(output), "ContentType": mime, "ChecksumSHA256": base64.b64encode(hashlib.sha256(output).digest()).decode(),
                "Metadata": {"media-id": str(asset.pk)}, "VersionId": "existing-v1"}
        with patch("evidence.tasks.s3.read_original", return_value=data), patch("evidence.s3.client", return_value=sdk), Stubber(sdk) as stub:
            stub.add_response("head_object", info, {"Bucket": asset.bucket, "Key": asset.object_key + ".optimized", "ChecksumMode": "ENABLED"})
            self.assertEqual(optimize_media(str(asset.pk)), "ready")
            stub.assert_no_pending_responses()  # No additional PUT/version.

    def test_transient_failure_backoff_final_failure_and_corrupt_image(self):
        asset, data = asset_fixture()
        with patch("evidence.tasks.s3.read_original", side_effect=s3.StorageUnavailable), patch("evidence.tasks.enqueue_image") as enqueue:
            for attempt in range(1, 4):
                MediaAsset.objects.filter(pk=asset.pk).update(next_processing_at=timezone.now() - timedelta(seconds=1))
                with self.captureOnCommitCallbacks(execute=True):
                    self.assertEqual(optimize_media(str(asset.pk)), "pending" if attempt < 3 else "failed")
                asset.refresh_from_db(); self.assertEqual(asset.processing_attempts, attempt)
                self.assertGreater(asset.next_processing_at, timezone.now())
            self.assertEqual(enqueue.call_count, 2); self.assertEqual(optimize_media(str(asset.pk)), "skipped")
        MediaAsset.objects.filter(pk=asset.pk).update(processing_status="pending", processing_attempts=0, next_processing_at=timezone.now())
        with patch("evidence.tasks.s3.read_original", return_value=b"broken"), patch("evidence.tasks.enqueue_image") as enqueue:
            self.assertEqual(optimize_media(str(asset.pk)), "failed"); enqueue.assert_not_called()

    def test_worker_loss_and_active_claim_are_bounded(self):
        asset, data = asset_fixture()
        asset.processing_status = "processing"; asset.next_processing_at = timezone.now() + timedelta(seconds=60); asset.save()
        with patch("evidence.tasks.s3.read_original") as read:
            self.assertEqual(optimize_media(str(asset.pk)), "skipped"); read.assert_not_called()
        asset.processing_attempts = 3; asset.next_processing_at = timezone.now(); asset.save()
        self.assertEqual(optimize_media(str(asset.pk)), "failed")

    def test_confirm_enqueues_after_commit_and_broker_outage_is_recovered(self):
        asset, _ = asset_fixture(); asset.status = "pending"; asset.upload_expires_at = timezone.now() + timedelta(minutes=3); asset.save()
        info = {"ContentLength": asset.size_bytes, "ContentType": asset.content_type, "ChecksumSHA256": asset.checksum_sha256,
                "Metadata": {"media-id": str(asset.pk)}}
        with patch("evidence.services.s3.inspect_object", return_value=info), patch("evidence.services.s3.mark_confirmed"), patch("evidence.tasks.optimize_media.apply_async", side_effect=ConnectionError):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(confirm_upload(actor=asset.uploaded_by, media_id=asset.pk).status, "ready")
        asset.refresh_from_db(); self.assertEqual(asset.processing_status, "pending")
        with patch("evidence.tasks.s3.read_original", return_value=image_bytes()), patch("evidence.tasks.s3.store_optimized", return_value=(asset.object_key + ".optimized", "v1")):
            self.assertEqual(sweep_images(), 1)
            self.assertEqual(sweep_images(), 0)
        asset.refresh_from_db(); self.assertEqual(asset.processing_status, "ready")

    def test_optimized_access_permission_fallback_and_original_option(self):
        asset, _ = asset_fixture(); from accounts.models import User
        other = User.objects.create_user(username="synthetic-other")
        client = APIClient(); client.force_authenticate(other)
        self.assertEqual(client.get(f"/api/v1/media/{asset.pk}/download/").status_code, 404)
        client.force_authenticate(asset.uploaded_by)
        with patch("evidence.services.s3.presign_download", return_value="https://synthetic.invalid/original"), patch("evidence.services.s3.presign_optimized", return_value="https://synthetic.invalid/optimized"):
            self.assertEqual(client.get(f"/api/v1/media/{asset.pk}/download/").data["variant"], "original")
            asset.processing_status = "ready"; asset.optimized_key = asset.object_key + ".optimized"; asset.save()
            self.assertEqual(client.get(f"/api/v1/media/{asset.pk}/download/").data["variant"], "optimized")
            self.assertEqual(client.get(f"/api/v1/media/{asset.pk}/download/?variant=original").data["variant"], "original")
            asset.processing_status = "failed"; asset.save()
            self.assertEqual(client.get(f"/api/v1/media/{asset.pk}/download/").data["variant"], "original")

    def test_video_is_unchanged_and_cleanup_serializes_with_processing(self):
        asset, _ = asset_fixture(b"synthetic video", "video/mp4")
        with patch("evidence.tasks.s3.read_original") as read:
            self.assertEqual(optimize_media(str(asset.pk)), "skipped"); read.assert_not_called()
        asset.content_type = "image/jpeg"; asset.processing_status = "processing"
        asset.processing_attempts = 1
        asset.next_processing_at = timezone.now() + timedelta(seconds=60)
        asset.status = "deleting"; asset.save()
        with patch("evidence.tasks.s3.delete_object") as original, patch("evidence.tasks.s3.delete_optimized") as optimized:
            self.assertEqual(cleanup_media(str(asset.pk)), "skipped"); original.assert_not_called(); optimized.assert_not_called()
            MediaAsset.objects.filter(pk=asset.pk).update(next_processing_at=timezone.now() - timedelta(seconds=1))
            self.assertEqual(cleanup_media(str(asset.pk)), "deleted"); original.assert_called_once(); optimized.assert_called_once()
            self.assertEqual(optimize_media(str(asset.pk)), "skipped")

    def test_lightweight_measured_benchmark(self):
        raw = image_bytes(size=(2400, 1800), noise=True); started = time.perf_counter()
        with override_settings(MEDIA_IMAGE_MAX_DIMENSION=1600):
            output, _, width, height = optimize_image(raw, "image/jpeg")
        duration = round((time.perf_counter() - started) * 1000, 1)
        print(f"IMAGE_BENCHMARK original_bytes={len(raw)} optimized_bytes={len(output)} reduction_pct={100*(1-len(output)/len(raw)):.1f} duration_ms={duration}")
        self.assertLess(len(output), len(raw)); self.assertEqual((width, height), (1600, 1200))


@override_settings(S3_BUCKET_NAME="synthetic-private-bucket")
class ImageRedisWorkerTests(TransactionTestCase):
    def test_worker_restart_consumes_queued_image_without_reprocessing_ready_media(self):
        asset, data = asset_fixture(); queue = "test-restart-images-" + uuid4().hex
        with patch("evidence.tasks.s3.read_original", return_value=data) as read, \
                patch("evidence.tasks.s3.store_optimized", side_effect=lambda media, *_: (media.object_key + ".optimized", "v1")):
            with start_worker(app, perform_ping_check=False, pool="solo", queues=[queue], shutdown_timeout=15):
                optimize_media.apply_async(args=[str(asset.pk)], queue=queue)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    asset.refresh_from_db()
                    if asset.processing_status == "ready": break
                    time.sleep(.05)
                self.assertEqual(asset.processing_status, "ready")
            # Queue while the worker is stopped; preserve already completed work.
            extra = MediaAsset.objects.create(report=asset.report, uploaded_by=asset.uploaded_by,
                bucket=asset.bucket, object_key="pending", filename=asset.filename,
                content_type=asset.content_type, size_bytes=asset.size_bytes, checksum_sha256=asset.checksum_sha256,
                status="ready", confirmed_at=timezone.now(), upload_expires_at=asset.upload_expires_at,
                next_cleanup_at=asset.next_cleanup_at, processing_status="pending", next_processing_at=timezone.now())
            extra.object_key = f"evidence/reports/{extra.report_id}/{extra.pk.hex}"; extra.save()
            optimize_media.apply_async(args=[str(asset.pk)], queue=queue)
            optimize_media.apply_async(args=[str(extra.pk)], queue=queue)
            with start_worker(app, perform_ping_check=False, pool="solo", queues=[queue], shutdown_timeout=15):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    extra.refresh_from_db()
                    if extra.processing_status == "ready": break
                    time.sleep(.05)
                self.assertEqual(extra.processing_status, "ready")
            self.assertEqual(read.call_count, 2)
            asset.refresh_from_db()
            self.assertEqual(asset.processing_attempts, 1)
            self.assertEqual(extra.processing_attempts, 1)

    def test_real_worker_optimizes_without_reading_production_s3(self):
        asset, data = asset_fixture(); queue = "test-images-" + uuid4().hex
        with patch("evidence.tasks.s3.read_original", return_value=data), patch("evidence.tasks.s3.store_optimized", return_value=(asset.object_key + ".optimized", "v1")):
            with start_worker(app, perform_ping_check=False, pool="solo", queues=[queue], shutdown_timeout=15):
                optimize_media.apply_async(args=[str(asset.pk)], queue=queue)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    asset.refresh_from_db()
                    if asset.processing_status == "ready": break
                    time.sleep(.05)
                self.assertEqual(asset.processing_status, "ready")
