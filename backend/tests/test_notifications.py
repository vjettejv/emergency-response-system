from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.gis.geos import Point
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from accounts.models import Role, User
from config.asgi import application
from dispatch.services import assign_team, transition_assignment, cancel_assignment, submit_signal
from incidents.models import Incident, IncidentCategory, IncidentReport
from incidents.services import submit_report, review_report, create_incident_from_report, change_incident_status
from notifications.models import Notification
from notifications.services import emit, incident_updated
from teams.models import ResponseTeam
from tests.test_realtime import TEST_LAYERS


def setup_domain(test):
    test.citizen = User.objects.create_user(username="synthetic-citizen")
    test.other = User.objects.create_user(username="synthetic-other")
    test.manager = User.objects.create_user(username="synthetic-dispatcher", role=Role.DISPATCHER)
    test.other_manager = User.objects.create_user(username="synthetic-other-dispatcher", role=Role.DISPATCHER)
    test.admin = User.objects.create_user(username="synthetic-admin", role=Role.ADMIN)
    test.category = IncidentCategory.objects.create(code="synthetic", name="Synthetic")
    test.team = ResponseTeam.objects.create(code="synthetic", name="Synthetic", status="available")
    test.team.categories.add(test.category)
    test.rescue = User.objects.create_user(username="synthetic-rescue", role=Role.RESCUE_TEAM, response_team=test.team)
    test.other_team = ResponseTeam.objects.create(code="synthetic-other", name="Synthetic other", status="available")
    test.outsider = User.objects.create_user(username="synthetic-outsider", role=Role.RESCUE_TEAM, response_team=test.other_team)
    test.incident = Incident.objects.create(title="Synthetic", category=test.category, location=Point(105, 21, srid=4326), status="verified")


class NotificationTests(APITestCase):
    def setUp(self):
        setup_domain(self)

    def report(self):
        return submit_report(actor=self.citizen, category=self.category, description="Synthetic private incident detail",
                             location=Point(105, 21, srid=4326), reporter_phone="+84900000000")

    def test_report_submission_review_and_link_are_deduplicated_and_private(self):
        report = self.report()
        self.assertEqual(Notification.objects.filter(type="new_report").count(), 2)
        self.assertEqual(Notification.objects.filter(recipient=self.citizen, type="report_received").count(), 1)
        review_report(actor=self.manager, report_id=report.pk, review_status="accepted")
        review_report(actor=self.manager, report_id=report.pk, review_status="accepted")
        self.assertEqual(Notification.objects.filter(recipient=self.citizen, type="report_verified").count(), 1)
        create_incident_from_report(actor=self.manager, report_id=report.pk, title="Synthetic")
        self.assertEqual(Notification.objects.filter(recipient=self.citizen, type="incident_created").count(), 1)
        self.assertFalse(Notification.objects.filter(recipient=self.admin).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.other).exists())
        for notification in Notification.objects.all():
            self.assertNotIn("+849", notification.message)
            self.assertNotIn("private incident detail", notification.message)

    def test_dispatch_lifecycle_notifies_only_assigned_team_and_responsible_dispatcher(self):
        assignment = assign_team(actor=self.manager, incident_id=self.incident.pk, team_id=self.team.pk)
        self.assertEqual(Notification.objects.filter(recipient=self.rescue, type="new_assignment").count(), 1)
        self.assertFalse(Notification.objects.filter(recipient=self.outsider).exists())
        for previous, target in [("pending", "accepted"), ("accepted", "en_route"), ("en_route", "on_scene"), ("on_scene", "responding"), ("responding", "completed")]:
            transition_assignment(actor=self.rescue, assignment_id=assignment.pk, status=target, expected_status=previous)
        for kind in ["assignment_accepted", "team_arrived", "assignment_completed"]:
            self.assertEqual(Notification.objects.filter(recipient=self.manager, type=kind).count(), 1)
            self.assertFalse(Notification.objects.filter(recipient=self.other_manager, type=kind).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.admin).exists())

    def test_support_retry_and_cancel_do_not_duplicate(self):
        assignment = assign_team(actor=self.manager, incident_id=self.incident.pk, team_id=self.team.pk)
        payload = {"request_id": str(uuid4()), "kind": "support", "support_type": "medical", "reported_at": timezone.now().isoformat(), "latitude": 21, "longitude": 105}
        submit_signal(actor=self.rescue, assignment_id=assignment.pk, data=payload)
        submit_signal(actor=self.rescue, assignment_id=assignment.pk, data=payload)
        self.assertEqual(Notification.objects.filter(type="assistance_requested", recipient=self.manager).count(), 1)
        cancel_assignment(actor=self.manager, assignment_id=assignment.pk, expected_status="pending")
        self.assertEqual(Notification.objects.filter(type="assignment_cancelled", recipient=self.rescue).count(), 1)

    def test_incident_resolution_groups_same_citizen_reports_once(self):
        for _ in range(2):
            IncidentReport.objects.create(reporter=self.citizen, category=self.category, incident=self.incident,
                                          description="Synthetic", location=Point(105, 21, srid=4326))
        self.incident.status = "in_progress"; self.incident.save()
        change_incident_status(actor=self.manager, incident_id=self.incident.pk, status="resolved", expected_status="in_progress")
        self.assertEqual(Notification.objects.filter(recipient=self.citizen, type="incident_resolved").count(), 1)
        history = self.incident.status_history.latest("pk")
        self.incident.refresh_from_db(); incident_updated(self.incident, history.pk)
        self.assertEqual(Notification.objects.filter(recipient=self.citizen, type="incident_resolved").count(), 1)

    def test_own_paginated_inbox_unread_and_read_commands(self):
        self.report(); self.client.force_authenticate(self.citizen)
        response = self.client.get("/api/v1/notifications/?page_size=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response["Cache-Control"], "no-store")
        item = response.data["results"][0]
        self.assertNotIn("recipient", item); self.assertNotIn("event_key", item)
        self.assertEqual(self.client.get("/api/v1/notifications/unread-count/").data["count"], 1)
        for path in [f"/api/v1/notifications/{item['id']}/read/"] * 2:
            self.assertEqual(self.client.post(path, {}, format="json").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/notifications/unread-count/").data["count"], 0)
        self.report()
        self.assertEqual(self.client.post("/api/v1/notifications/read-all/", {}, format="json").data["updated"], 1)
        self.assertEqual(self.client.post("/api/v1/notifications/read-all/", {}, format="json").data["updated"], 0)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f"/api/v1/notifications/{item['id']}/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/notifications/{item['id']}/read/", {}, format="json").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/notifications/?recipient={self.citizen.pk}").status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/notifications/").status_code, 401)

    def test_commit_only_delivery_and_rollback(self):
        with patch("realtime.events.deliver", new_callable=AsyncMock) as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                emit([self.citizen.pk], Notification.Type.REPORT_RECEIVED, "Synthetic", "Synthetic", "test:commit")
                deliver.assert_not_called()
            self.assertEqual(deliver.call_args.args[0], f"user_{self.citizen.pk}")
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        emit([self.citizen.pk], Notification.Type.REPORT_RECEIVED, "Synthetic", "Synthetic", "test:rollback")
                        raise ValueError("synthetic rollback")
                except ValueError:
                    pass
            self.assertEqual(deliver.call_count, 1)
            self.assertFalse(Notification.objects.filter(event_key="test:rollback").exists())

    def test_camera_upload_to_dispatch_and_resolution_end_to_end(self):
        import base64
        import hashlib
        from evidence.services import create_upload, confirm_upload, download_url
        from evidence.tasks import optimize_media
        from incidents.services import save_report_draft, finalize_report
        from tests.test_image_processing import image_bytes
        raw = image_bytes()
        draft = save_report_draft(actor=self.citizen, request_id=uuid4(), values={
            "category": self.category, "description": "Synthetic camera report", "location": Point(105, 21, srid=4326),
            "reporter_name": "Synthetic", "reporter_phone": "+84900000000"})
        with override_settings(S3_BUCKET_NAME="synthetic-private-bucket"), patch("evidence.services.s3.presign_upload", return_value={"url": "https://synthetic.invalid/upload"}):
            asset, _ = create_upload(actor=self.citizen, data={"report_id": draft.pk, "filename": "scene.jpg", "content_type": "image/jpeg",
                "size_bytes": len(raw), "checksum_sha256": base64.b64encode(hashlib.sha256(raw).digest()).decode(),
                "capture_source": "camera", "captured_at": timezone.now().isoformat()})
            self.assertFalse(Notification.objects.exists())
            info = {"ContentLength": len(raw), "ContentType": "image/jpeg", "ChecksumSHA256": asset.checksum_sha256, "Metadata": {"media-id": str(asset.pk)}}
            with patch("evidence.services.s3.inspect_object", return_value=info), patch("evidence.services.s3.mark_confirmed"):
                confirm_upload(actor=self.citizen, media_id=asset.pk)
            # Submission is independent of background completion.
            finalize_report(actor=self.citizen, report_id=draft.pk)
            self.assertTrue(Notification.objects.filter(recipient=self.manager, type="new_report").exists())
            with patch("evidence.tasks.s3.read_original", return_value=raw), patch("evidence.tasks.s3.store_optimized", return_value=(asset.object_key + ".optimized", "v1")):
                self.assertEqual(optimize_media(str(asset.pk)), "ready")
            review_report(actor=self.manager, report_id=draft.pk, review_status="accepted")
            incident = create_incident_from_report(actor=self.manager, report_id=draft.pk, title="Synthetic incident")
            with patch("evidence.services.s3.presign_optimized", return_value="https://synthetic.invalid/optimized"):
                self.assertEqual(download_url(actor=self.manager, media_id=asset.pk)["variant"], "optimized")
            assignment = assign_team(actor=self.manager, incident_id=incident.pk, team_id=self.team.pk)
            for previous, target in [("pending", "accepted"), ("accepted", "en_route"), ("en_route", "on_scene"), ("on_scene", "responding"), ("responding", "completed")]:
                transition_assignment(actor=self.rescue, assignment_id=assignment.pk, status=target, expected_status=previous)
            change_incident_status(actor=self.manager, incident_id=incident.pk, status="resolved", expected_status="in_progress")
            self.assertEqual(Notification.objects.filter(recipient=self.citizen, type="incident_resolved").count(), 1)
            self.assertEqual(Notification.objects.filter(recipient=self.rescue, type="new_assignment").count(), 1)
            self.assertEqual(Notification.objects.filter(recipient=self.manager, type="team_arrived").count(), 1)


@override_settings(CHANNEL_LAYERS=TEST_LAYERS)
class NotificationSocketTests(TransactionTestCase):
    def setUp(self):
        setup_domain(self)
        self.tokens = {u.pk: Token.objects.create(user=u).key for u in [self.citizen, self.manager, self.rescue, self.outsider]}

    @asynccontextmanager
    async def connected(self, user):
        client = WebsocketCommunicator(application, "/ws/notifications/", headers=[(b"origin", b"http://localhost:8000")])
        try:
            self.assertTrue((await client.connect())[0])
            await client.send_json_to({"type": "authenticate", "token": self.tokens[user.pk]})
            self.assertEqual((await client.receive_json_from())["type"], "ready")
            yield client
        finally:
            await client.disconnect()

    async def test_recipient_delivery_resync_no_duplicates_and_group_injection(self):
        async with self.connected(self.citizen) as citizen, self.connected(self.outsider) as outsider:
            await database_sync_to_async(emit)([self.citizen.pk], Notification.Type.REPORT_RECEIVED, "Synthetic", "Synthetic", "ws:one")
            event = await citizen.receive_json_from()
            self.assertEqual(event["type"], "notification.created")
            self.assertEqual(set(event["data"]), {"id", "type", "title", "message", "created_at", "read_at", "related_entity"})
            self.assertTrue(await outsider.receive_nothing(timeout=.1))
            await outsider.send_json_to({"type": "subscribe", "group": f"user_{self.citizen.pk}"})
            self.assertEqual((await outsider.receive_json_from())["type"], "error")
        async with self.connected(self.citizen) as citizen:
            await database_sync_to_async(emit)([self.citizen.pk], Notification.Type.REPORT_RECEIVED, "Synthetic", "Synthetic", "ws:one")
            self.assertTrue(await citizen.receive_nothing(timeout=.1))
            self.assertEqual(await database_sync_to_async(Notification.objects.filter(recipient=self.citizen).count)(), 1)

    async def test_assignment_notification_and_read_reach_only_own_socket(self):
        from notifications.services import mark_read
        async with self.connected(self.rescue) as rescue, self.connected(self.outsider) as outsider:
            await database_sync_to_async(assign_team)(actor=self.manager, incident_id=self.incident.pk, team_id=self.team.pk)
            data = (await rescue.receive_json_from())["data"]
            self.assertEqual(data["type"], "new_assignment")
            await database_sync_to_async(mark_read)(self.rescue, data["id"])
            self.assertEqual((await rescue.receive_json_from())["type"], "notification.read")
            self.assertTrue(await outsider.receive_nothing(timeout=.1))
