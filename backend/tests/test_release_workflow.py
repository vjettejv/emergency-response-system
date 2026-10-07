"""REST commands, persisted state and Redis events in one disposable workflow.

Only the external S3 transport and broker enqueue boundary are stubbed. Domain
services, authorization, database, Redis transport and Pillow run normally.
"""
import base64
import hashlib
from contextlib import asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.gis.geos import Point
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from config.asgi import application
from dispatch.models import Assignment
from evidence.models import MediaAsset
from evidence.tasks import optimize_media
from incidents.models import IncidentReport
from notifications.models import Notification
from tests.test_image_processing import image_bytes
from tests.test_notifications import setup_domain
from tests.test_realtime import TEST_LAYERS


@override_settings(CHANNEL_LAYERS=TEST_LAYERS, S3_BUCKET_NAME="synthetic-private-bucket")
class ReleaseWorkflowTests(TransactionTestCase):
    def setUp(self):
        setup_domain(self)
        self.team.last_location = Point(105, 21, srid=4326)
        self.team.save()
        self.tokens = {user.pk: Token.objects.create(user=user).key
                       for user in (self.citizen, self.manager, self.rescue)}

    @database_sync_to_async
    def request(self, actor, method, path, data=None, expected=200):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Token " + self.tokens[actor.pk])
        response = getattr(client, method)("/api/v1/" + path, data, format="json")
        self.assertEqual(response.status_code, expected, response.data)
        return response.data

    @asynccontextmanager
    async def socket(self, actor, path):
        client = WebsocketCommunicator(application, path, headers=[(b"origin", b"http://localhost:8000")])
        try:
            self.assertTrue((await client.connect())[0])
            await client.send_json_to({"type": "authenticate", "token": self.tokens[actor.pk]})
            self.assertEqual((await client.receive_json_from())["type"], "ready")
            yield client
        finally:
            await client.disconnect()

    async def event(self, client, kind, **matching):
        for _ in range(25):
            event = await client.receive_json_from(timeout=3)
            if event["type"] == kind and all(event["data"].get(key) == value for key, value in matching.items()):
                self.assertNotIn("reporter_phone", event["data"])
                return event
        self.fail("Expected workflow event was not delivered")

    @database_sync_to_async
    def final_state(self, report_id, media_id, assignment_id):
        report = IncidentReport.objects.select_related("incident").get(pk=report_id)
        asset = MediaAsset.objects.get(pk=media_id)
        assignment = Assignment.objects.get(pk=assignment_id)
        self.team.refresh_from_db()
        self.assertEqual((report.location.x, report.location.y, report.location.srid), (105, 21, 4326))
        self.assertEqual((self.team.last_location.x, self.team.last_location.y, self.team.last_location.srid), (105.001, 21.001, 4326))
        self.assertEqual(report.incident.status, "resolved")
        self.assertEqual((asset.status, asset.processing_status), ("ready", "ready"))
        for name in ("accepted_at", "en_route_at", "arrived_at", "responding_at", "completed_at"):
            self.assertIsNotNone(getattr(assignment, name))
        self.assertEqual(Notification.objects.filter(recipient=self.citizen, type="incident_resolved").count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.rescue, type="new_assignment").count(), 1)

    async def test_camera_report_rest_dispatch_gps_resolution_with_live_redis_events(self):
        async with self.socket(self.manager, "/ws/dispatcher/") as manager, \
                self.socket(self.rescue, "/ws/rescue/") as rescue, \
                self.socket(self.citizen, "/ws/notifications/") as inbox:
            raw = image_bytes()
            draft = await self.request(self.citizen, "post", "incident-reports/drafts/", {
                "request_id": str(uuid4()), "category": self.category.pk, "description": "Synthetic camera report",
                "latitude": 21, "longitude": 105, "reporter_name": "Synthetic", "reporter_phone": "+84900000000",
            }, expected=201)
            report_id = draft["id"]
            with patch("evidence.services.s3.presign_upload", return_value={"url": "https://synthetic.invalid/upload"}):
                upload = await self.request(self.citizen, "post", "media/presign/", {
                    "report_id": report_id, "filename": "scene.jpg", "content_type": "image/jpeg",
                    "size_bytes": len(raw), "checksum_sha256": base64.b64encode(hashlib.sha256(raw).digest()).decode(),
                    "capture_source": "camera", "captured_at": timezone.now().isoformat(),
                }, expected=201)
            media_id = upload["media"]["id"]
            info = {"ContentLength": len(raw), "ContentType": "image/jpeg",
                    "ChecksumSHA256": base64.b64encode(hashlib.sha256(raw).digest()).decode(), "Metadata": {"media-id": media_id}}
            with patch("evidence.services.s3.inspect_object", return_value=info), \
                    patch("evidence.services.s3.mark_confirmed"), patch("evidence.tasks.enqueue_image") as enqueue:
                await self.request(self.citizen, "post", f"media/{media_id}/confirm/", {})
                enqueue.assert_called_once()
            with patch("evidence.tasks.s3.read_original", return_value=raw), \
                    patch("evidence.tasks.s3.store_optimized", return_value=("synthetic-optimized", "v1")):
                self.assertEqual(await database_sync_to_async(optimize_media)(media_id), "ready")
            await self.request(self.citizen, "post", f"incident-reports/{report_id}/submit/", {})
            await self.event(manager, "report.changed", report_id=report_id)
            await self.event(inbox, "notification.created")
            await self.request(self.manager, "post", f"incident-reports/{report_id}/verify/", {"review_status": "accepted"})
            await self.request(self.manager, "get", f"incident-reports/{report_id}/potential-duplicates/")
            incident = await self.request(self.manager, "post", f"incident-reports/{report_id}/create-incident/",
                                          {"title": "Synthetic response"}, expected=201)
            incident_id = incident["id"]
            suggestions = await self.request(self.manager, "get", f"incidents/{incident_id}/suggested-teams/")
            self.assertEqual(suggestions["teams"][0]["id"], self.team.pk)
            assignment = await self.request(self.manager, "post", f"incidents/{incident_id}/assignments/",
                                            {"team_id": self.team.pk}, expected=201)
            assignment_id = assignment["id"]
            await self.event(rescue, "assignment.status_changed", assignment_id=assignment_id, status="assigned")
            contact = await self.request(self.rescue, "get", f"incident-reports/{report_id}/contact/")
            self.assertTrue(contact["can_call"])
            await self.request(self.rescue, "post", f"assignments/{assignment_id}/accept/", {"expected_status": "assigned"})
            await self.event(manager, "assignment.status_changed", assignment_id=assignment_id, status="accepted")
            for previous, target in (("accepted", "en_route"), ("en_route", "on_scene"), ("on_scene", "responding"), ("responding", "completed")):
                await self.request(self.rescue, "patch", f"assignments/{assignment_id}/status/", {"expected_status": previous, "status": target})
                await self.event(manager, "assignment.status_changed", assignment_id=assignment_id, status=target)
                await self.event(rescue, "assignment.status_changed", assignment_id=assignment_id, status=target)
                if target == "en_route":
                    await self.request(self.rescue, "post", "teams/me/location/", {
                        "latitude": 21.001, "longitude": 105.001, "accuracy": 5, "timestamp": timezone.now().isoformat()})
                    await self.event(manager, "team.location_updated", team_id=self.team.pk)
            await self.request(self.manager, "patch", f"incidents/{incident_id}/status/", {"expected_status": "in_progress", "status": "resolved"})
            await self.event(manager, "incident.status_changed", incident_id=incident_id, status="resolved")
            report = await self.request(self.citizen, "get", f"incident-reports/{report_id}/")
            self.assertEqual(report["incident_status"], "resolved")
            inbox_data = await self.request(self.citizen, "get", "notifications/")
            self.assertIn("incident_resolved", [item["type"] for item in inbox_data["results"]])
            await self.final_state(report_id, media_id, assignment_id)
