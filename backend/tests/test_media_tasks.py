import time
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from celery.contrib.testing.worker import start_worker
from django.contrib.gis.geos import Point
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from accounts.models import User
from config.celery import app
from evidence.models import MediaAsset
from evidence.s3 import StorageUnavailable
from evidence.tasks import cleanup_media, sweep_media
from incidents.models import IncidentCategory, IncidentReport


def fixture():
    user=User.objects.create_user(username="synthetic")
    category=IncidentCategory.objects.create(code="synthetic",name="Synthetic")
    report=IncidentReport.objects.create(reporter=user,category=category,description="Synthetic",location=Point(105,21,srid=4326))
    return MediaAsset.objects.create(report=report,uploaded_by=user,filename="test.jpg",content_type="image/jpeg",
        size_bytes=3,checksum_sha256="synthetic",bucket="synthetic",object_key=f"synthetic/{uuid4()}",
        upload_expires_at=timezone.now()-timedelta(minutes=10),next_cleanup_at=timezone.now()-timedelta(seconds=1))


class CleanupTests(TestCase):
    def setUp(self):
        self.asset=fixture()

    def test_success_duplicate_delivery_and_no_delete_before_expiry(self):
        with patch("evidence.tasks.s3.delete_object") as delete:
            self.assertEqual(cleanup_media(str(self.asset.pk)),"deleted")
            self.assertEqual(cleanup_media(str(self.asset.pk)),"skipped")
            delete.assert_called_once()
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.cleanup_attempts,1)
        self.asset.status="pending";self.asset.next_cleanup_at=timezone.now()+timedelta(minutes=5);self.asset.save()
        with patch("evidence.tasks.s3.delete_object") as delete:
            self.assertEqual(cleanup_media(str(self.asset.pk)),"skipped")
            delete.assert_not_called()

    def test_failure_backoff_and_terminal_failure_after_five_attempts(self):
        with patch("evidence.tasks.s3.delete_object",side_effect=StorageUnavailable), patch("evidence.tasks.enqueue_cleanup") as enqueue:
            for attempt in range(1,6):
                MediaAsset.objects.filter(pk=self.asset.pk).update(next_cleanup_at=timezone.now()-timedelta(seconds=1))
                with self.captureOnCommitCallbacks(execute=True):
                    result=cleanup_media(str(self.asset.pk))
                self.asset.refresh_from_db()
                self.assertEqual(self.asset.cleanup_attempts,attempt)
                self.assertEqual(result,"failed" if attempt==5 else "deleting")
                self.assertGreater(self.asset.next_cleanup_at,timezone.now())
            self.assertEqual(enqueue.call_count,4)
            self.assertEqual(cleanup_media(str(self.asset.pk)),"skipped")

    def test_sweep_recovers_expired_upload_and_missed_delete_message(self):
        with patch("evidence.tasks.s3.delete_object"):
            self.assertEqual(sweep_media(),1)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status,"deleted")

    def test_ready_media_is_never_swept(self):
        self.asset.status="ready";self.asset.confirmed_at=timezone.now();self.asset.save()
        with patch("evidence.tasks.s3.delete_object") as delete:
            self.assertEqual(sweep_media(),0)
            self.assertEqual(cleanup_media(str(self.asset.pk)),"skipped")
            delete.assert_not_called()


class RedisWorkerTests(TransactionTestCase):
    def test_real_redis_worker_consumes_cleanup_task(self):
        asset=fixture()
        queue=f"test-media-{uuid4().hex}"
        with patch("evidence.tasks.s3.delete_object") as delete:
            with start_worker(app,perform_ping_check=False,pool="solo",queues=[queue],shutdown_timeout=15):
                cleanup_media.apply_async(args=[str(asset.pk)],queue=queue)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    asset.refresh_from_db()
                    if asset.status=="deleted":
                        break
                    time.sleep(0.05)
                self.assertEqual(asset.status,"deleted")
            delete.assert_called_once()
