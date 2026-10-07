"""Release regressions on synthetic data, never production accounts or objects."""
import io
import os
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.conf import settings
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import Role, User
from dispatch.models import Assignment
from incidents.models import IncidentReport
from tests.test_notifications import setup_domain
from evidence.images import optimize_image


class ReleaseRegressionTests(TestCase):
    def setUp(self):
        cache.clear()
        setup_domain(self)
        self.client = APIClient()

    def test_assignment_list_query_count_does_not_grow_with_page_size(self):
        IncidentReport.objects.create(reporter=self.citizen, category=self.category,
                                      location=self.incident.location, incident=self.incident)
        Assignment.objects.bulk_create([Assignment(incident=self.incident, team=self.team,
            assigned_by=self.manager, status="completed", ended_at=timezone.now() + timedelta(seconds=1))
            for _ in range(10)])
        self.client.force_authenticate(self.manager)
        counts = []
        for size in (1, 10):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(f"/api/v1/assignments/?page_size={size}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.data["results"]), size)
                self.assertIsNotNone(response.data["results"][0]["timeline"]["report_created_at"])
            counts.append(len(queries))
        self.assertEqual(counts[0], counts[1], counts)
        self.assertLessEqual(counts[1], 3)

    @override_settings(MEDIA_IMAGE_OUTPUT_FORMAT="WEBP")
    def test_paletted_png_transparency_is_preserved_in_webp(self):
        image = Image.new("P", (20, 20), 0)
        image.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
        image.info["transparency"] = 0
        image.putpixel((10, 10), 1)
        source = io.BytesIO()
        image.save(source, format="PNG")
        output, _, _, _ = optimize_image(source.getvalue(), "image/png")
        with Image.open(io.BytesIO(output)) as result:
            self.assertEqual(result.convert("RGBA").getpixel((0, 0))[3], 0)
            self.assertEqual(result.convert("RGBA").getpixel((10, 10))[3], 255)

    @override_settings(PRODUCTION=True)
    def test_demo_seed_refuses_production_before_creating_users(self):
        before = User.objects.count()
        with patch.dict(os.environ, {"DEMO_PASSWORD": "Synthetic-only-release-pass-42!"}):
            with self.assertRaises(CommandError):
                call_command("seed_demo", stdout=io.StringIO())
        self.assertEqual(User.objects.count(), before)

    def test_idor_matrix_and_no_store_for_contact_and_media(self):
        from evidence.models import MediaAsset
        from notifications.models import Notification
        from django.utils import timezone
        report = IncidentReport.objects.create(reporter=self.citizen, category=self.category,
            incident=self.incident, location=self.incident.location,
            reporter_phone="+12025550123", allow_contact=True)
        assignment = Assignment.objects.create(incident=self.incident, team=self.team, assigned_by=self.manager)
        media = MediaAsset.objects.create(report=report, uploaded_by=self.citizen, bucket="synthetic",
            object_key="synthetic", filename="scene.jpg", content_type="image/jpeg", size_bytes=10,
            checksum_sha256="synthetic", upload_expires_at=timezone.now(), next_cleanup_at=timezone.now())
        item = Notification.objects.create(recipient=self.citizen, type="report_received", title="Synthetic",
            message="Synthetic", event_key="synthetic")
        for actor in (self.other, self.outsider):
            self.client.force_authenticate(actor)
            for path in (f"incident-reports/{report.pk}/", f"incident-reports/{report.pk}/contact/",
                         f"incidents/{self.incident.pk}/", f"assignments/{assignment.pk}/",
                         f"notifications/{item.pk}/", f"media/{media.pk}/download/"):
                self.assertIn(self.client.get("/api/v1/" + path).status_code, (403, 404), path)
        self.client.force_authenticate(self.rescue)
        response = self.client.get(f"/api/v1/incident-reports/{report.pk}/contact/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["phone_masked"])
        self.assertEqual(response["Cache-Control"], "no-store")
        Assignment.objects.filter(pk=assignment.pk).update(status="cancelled", ended_at=timezone.now())
        self.assertEqual(self.client.get(f"/api/v1/incident-reports/{report.pk}/contact/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/media/{media.pk}/download/").status_code, 404)

    def test_malformed_and_unexpected_report_inputs_leave_database_unchanged(self):
        self.client.force_authenticate(self.citizen)
        path = "/api/v1/incident-reports/"
        self.assertEqual(self.client.post(path, "{broken", content_type="application/json").status_code, 400)
        payload = {"category": self.category.pk, "description": "Synthetic", "latitude": 21, "longitude": 105,
                   "reporter_name": "Synthetic", "reporter_phone": "+12025550123"}
        for changes in ({"latitude": 91}, {"longitude": float("inf")}, {"category": "bad"},
                        {"description": "x" * 10001}, {"internal_key": "forged"}, {"occurred_at": "yesterday"}):
            # Encode non-finite input as a string to exercise coordinate validation, not JSON renderer.
            body = {**payload, **changes}
            if body["longitude"] == float("inf"):
                body["longitude"] = "Infinity"
            self.assertEqual(self.client.post(path, body, format="json").status_code, 400, changes.keys())
        self.assertFalse(IncidentReport.objects.exists())

    def test_report_command_throttles_and_cache_outage_fails_closed(self):
        self.client.force_authenticate(self.citizen)
        config = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {
            **settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "report_write": "2/min"}}
        payload = {"category": self.category.pk, "description": "Synthetic", "latitude": 21, "longitude": 105,
                   "reporter_name": "Synthetic", "reporter_phone": "+12025550123"}
        with override_settings(REST_FRAMEWORK=config):
            for _ in range(2):
                self.assertEqual(self.client.post("/api/v1/incident-reports/", payload, format="json").status_code, 201)
            response = self.client.post("/api/v1/incident-reports/", payload, format="json")
            self.assertEqual(response.status_code, 429)
            self.assertIn("Retry-After", response)
            with patch("django.core.cache.backends.locmem.LocMemCache.get", side_effect=ConnectionError("synthetic-private")):
                response = self.client.post("/api/v1/incident-reports/", payload, format="json")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("synthetic-private", str(response.data))
        self.assertEqual(IncidentReport.objects.count(), 2)

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024)
    def test_json_body_limit_rejects_oversized_report_without_writing(self):
        self.client.force_authenticate(self.citizen)
        payload = {"category": self.category.pk, "description": "Synthetic",
                   "latitude": 21, "longitude": 105,
                   "reporter_name": "Synthetic", "reporter_phone": "+12025550123"}
        path = "/api/v1/incident-reports/"
        self.assertEqual(self.client.post(path, payload, format="json").status_code, 201)
        response = self.client.post(path, {**payload, "description": "x" * 2048}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(IncidentReport.objects.count(), 1)

    def test_media_and_notification_rate_guards_are_independent_of_reports(self):
        config = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {
            **settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "media_upload": "1/min", "notification": "1/min"}}
        self.client.force_authenticate(self.citizen)
        with override_settings(REST_FRAMEWORK=config):
            self.assertEqual(self.client.post("/api/v1/media/presign/", {}, format="json").status_code, 400)
            self.assertEqual(self.client.post("/api/v1/media/presign/", {}, format="json").status_code, 429)
            self.assertEqual(self.client.get("/api/v1/notifications/").status_code, 200)
            self.assertEqual(self.client.get("/api/v1/notifications/unread-count/").status_code, 429)
            self.assertEqual(self.client.get("/api/v1/incident-reports/").status_code, 200)

    def test_sql_timeline_matches_service_for_missing_and_recorded_verification(self):
        from dispatch.timeline import assignment_timeline, with_source_times
        from incidents.models import IncidentStatusHistory
        assignment = Assignment.objects.create(incident=self.incident, team=self.team, assigned_by=self.manager)
        self.assertEqual(assignment_timeline(with_source_times(Assignment.objects.all()).get(pk=assignment.pk)), assignment_timeline(assignment))
        report = IncidentReport.objects.create(reporter=self.citizen, category=self.category,
            incident=self.incident, location=self.incident.location, review_status="accepted")
        history = IncidentStatusHistory.objects.create(incident=self.incident, from_status="new", to_status="verified", changed_by=self.manager)
        self.assertEqual(assignment_timeline(with_source_times(Assignment.objects.all()).get(pk=assignment.pk))["verified_at"], history.changed_at)
        report.reviewed_at = timezone.now(); report.save()
        self.assertEqual(assignment_timeline(with_source_times(Assignment.objects.all()).get(pk=assignment.pk)), assignment_timeline(assignment))

    def test_cancellation_does_not_erase_recorded_acceptance_from_metrics(self):
        from dispatch.services import assign_team, transition_assignment, cancel_assignment
        from dispatch.timeline import response_metrics
        IncidentReport.objects.create(reporter=self.citizen, category=self.category, incident=self.incident,
                                      location=self.incident.location)
        assignment = assign_team(actor=self.manager, incident_id=self.incident.pk, team_id=self.team.pk)
        transition_assignment(actor=self.rescue, assignment_id=assignment.pk, expected_status="pending", status="accepted")
        cancel_assignment(actor=self.manager, assignment_id=assignment.pk, expected_status="accepted")
        metrics = response_metrics()
        self.assertEqual(metrics["mtta_sample_count"], 1)
        self.assertIsNotNone(metrics["mtta_seconds"])
        self.assertEqual(metrics["mttr_sample_count"], 0)
