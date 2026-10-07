"""Real independent PostgreSQL connections for release race scenarios A/F/G."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import AsyncMock, patch
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone
from datetime import timedelta

from evidence.models import MediaAsset
from evidence.services import confirm_upload, delete_media
from evidence.tasks import optimize_media, cleanup_media
from incidents.models import IncidentReport
from incidents.services import Conflict, review_report
from notifications.models import Notification
from notifications.services import emit
from tests.test_image_processing import asset_fixture
from tests.test_notifications import setup_domain


class ReleaseConcurrencyTests(TransactionTestCase):
    def setUp(self):
        setup_domain(self)
        self.category.code = "release-synthetic"
        self.category.save(update_fields=["code"])

    def race(self, operations):
        barrier = Barrier(len(operations))

        def run(operation):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=10)
                try:
                    operation()
                    return "ok"
                except Conflict:
                    return "conflict"
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            return list(pool.map(run, operations, timeout=20))

    def test_two_dispatchers_review_conflict_keeps_one_decision(self):
        report = IncidentReport.objects.create(reporter=self.citizen, category=self.category,
                                              location=self.incident.location)
        with patch("realtime.events.deliver", new_callable=AsyncMock):
            result = self.race([
                lambda: review_report(actor=self.manager, report_id=report.pk, review_status="accepted"),
                lambda: review_report(actor=self.other_manager, report_id=report.pk, review_status="rejected"),
            ])
        self.assertCountEqual(result, ["ok", "conflict"])
        report.refresh_from_db()
        self.assertIn(report.reviewed_by_id, (self.manager.pk, self.other_manager.pk))
        self.assertIsNotNone(report.reviewed_at)
        self.assertEqual(Notification.objects.filter(recipient=self.citizen).count(), 1)

    def test_two_confirmations_do_one_head_tag_and_enqueue(self):
        asset, _ = asset_fixture()
        MediaAsset.objects.filter(pk=asset.pk).update(status="pending", upload_expires_at=timezone.now() + timedelta(minutes=5))
        info = {"ContentLength": asset.size_bytes, "ContentType": asset.content_type,
                "ChecksumSHA256": asset.checksum_sha256, "Metadata": {"media-id": str(asset.pk)}}
        with patch("evidence.services.s3.inspect_object", return_value=info) as head, \
                patch("evidence.services.s3.mark_confirmed") as tag, patch("evidence.tasks.enqueue_image") as enqueue, \
                patch("realtime.events.deliver", new_callable=AsyncMock):
            result = self.race([lambda: confirm_upload(actor=asset.uploaded_by, media_id=asset.pk)] * 2)
        self.assertEqual(result, ["ok", "ok"])
        head.assert_called_once(); tag.assert_called_once(); enqueue.assert_called_once()
        asset.refresh_from_db()
        self.assertEqual(asset.status, "ready")
        self.assertEqual(asset.processing_status, "pending")

    def test_retried_notification_persists_and_publishes_once(self):
        with patch("realtime.events.deliver", new_callable=AsyncMock) as deliver:
            result = self.race([lambda: emit([self.citizen.pk], "report_received", "Synthetic", "Synthetic", "race:event")] * 2)
        self.assertEqual(result, ["ok", "ok"])
        self.assertEqual(Notification.objects.filter(event_key="race:event").count(), 1)
        deliver.assert_awaited_once()

    def test_delete_waits_for_processing_then_cleans_both_objects(self):
        asset, data = asset_fixture()
        entered, release, deleting = Event(), Event(), Event()

        def read(_):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("Processing test timed out")
            return data

        def process():
            close_old_connections()
            try:
                return optimize_media(str(asset.pk))
            finally:
                connections.close_all()

        def remove():
            close_old_connections()
            try:
                deleting.set()
                delete_media(actor=asset.uploaded_by, media_id=asset.pk)
            finally:
                connections.close_all()

        with patch("evidence.tasks.s3.read_original", side_effect=read), \
                patch("evidence.tasks.s3.store_optimized", return_value=(asset.object_key + ".optimized", "v1")), \
                patch("evidence.tasks.enqueue_cleanup"), patch("realtime.events.deliver", new_callable=AsyncMock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                worker = pool.submit(process)
                self.assertTrue(entered.wait(5))
                deletion = pool.submit(remove)
                self.assertTrue(deleting.wait(5))
                self.assertFalse(deletion.done())
                release.set()
                self.assertEqual(worker.result(10), "ready")
                deletion.result(10)
        asset.refresh_from_db()
        self.assertEqual(asset.status, "deleting")
        self.assertEqual(asset.processing_status, "ready")
        MediaAsset.objects.filter(pk=asset.pk).update(next_cleanup_at=timezone.now())
        with patch("evidence.tasks.s3.delete_object") as original, patch("evidence.tasks.s3.delete_optimized") as optimized:
            self.assertEqual(cleanup_media(str(asset.pk)), "deleted")
            original.assert_called_once(); optimized.assert_called_once()
