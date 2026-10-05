"""Synthetic operational flow: scoped contact, camera metadata and field commands."""
import base64
import hashlib
import importlib
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.contrib.gis.geos import Point
from django.db import connection, transaction, IntegrityError
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User
from dispatch.models import Assignment, AssignmentHistory, AssignmentSignal
from dispatch.services import assign_team, transition_assignment, submit_signal
from incidents.models import ContactAccess, IncidentReport, IncidentCategory
from incidents.services import create_incident_from_report, review_report, change_incident_status, Conflict
from teams.models import ResponseTeam
from evidence.models import MediaAsset


class FieldOperationsTests(APITestCase):
    def setUp(self):
        self.citizen = User.objects.create_user(username="synthetic-citizen")
        self.other = User.objects.create_user(username="synthetic-other")
        self.dispatcher = User.objects.create_user(username="synthetic-dispatcher", role=Role.DISPATCHER)
        self.admin = User.objects.create_user(username="synthetic-admin", role=Role.ADMIN)
        self.category = IncidentCategory.objects.create(code="synthetic-traffic", name="Synthetic traffic")
        self.team = ResponseTeam.objects.create(code="synthetic-team", name="Synthetic team", status="available")
        self.team.categories.add(self.category)
        self.rescue = User.objects.create_user(username="synthetic-rescue", role=Role.RESCUE_TEAM, response_team=self.team)
        self.outsider = User.objects.create_user(username="synthetic-outsider", role=Role.RESCUE_TEAM)
        self.report = IncidentReport.objects.create(reporter=self.citizen, category=self.category,
            description="Synthetic, not an emergency", location=Point(105, 21, srid=4326),
            reporter_name="Synthetic Citizen", reporter_phone="+84900000000", allow_contact=True, location_accuracy=250)
        review_report(actor=self.dispatcher, report_id=self.report.pk, review_status="accepted")
        self.incident = create_incident_from_report(actor=self.dispatcher, report_id=self.report.pk, title="Synthetic incident")
        self.assignment = assign_team(actor=self.dispatcher, incident_id=self.incident.pk, team_id=self.team.pk)
        self.report.refresh_from_db()
        self.client.force_authenticate(self.dispatcher)

    def endpoint(self, suffix="contact/"):
        return f"/api/v1/incident-reports/{self.report.pk}/{suffix}"

    def transition(self, status):
        self.assignment.refresh_from_db()
        return transition_assignment(actor=self.rescue, assignment_id=self.assignment.pk,
            status=status, expected_status=self.assignment.status)

    def signal(self, **overrides):
        return {"request_id": str(uuid4()), "kind": "support", "support_type": "medical",
            "latitude": 21, "longitude": 105, "accuracy": 5,
            "reported_at": timezone.now().isoformat(), "note": "Synthetic request", **overrides}

    def test_report_contact_validation_and_low_accuracy_does_not_block(self):
        self.client.force_authenticate(self.citizen)
        body = {"category": self.category.pk, "description": "Synthetic report", "latitude": 21,
            "longitude": 105, "reporter_name": "Synthetic", "reporter_phone": "(+84) 900 000 000",
            "allow_contact": True, "location_accuracy": 250}
        response = self.client.post("/api/v1/incident-reports/", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        report = IncidentReport.objects.get(pk=response.data["id"])
        self.assertEqual(report.reporter_phone, "+84900000000")
        self.assertEqual(report.location_accuracy, 250)
        self.assertEqual((report.location.x, report.location.y), (105, 21))
        self.assertNotEqual(response.data["reporter_phone"], report.reporter_phone)
        for bad in ["abc", "tel:+84900000000", "123", "+" + "9" * 16]:
            self.assertEqual(self.client.post("/api/v1/incident-reports/", {**body, "reporter_phone": bad}, format="json").status_code, 400)
        for changes in [{"location_accuracy": -1}, {"location_accuracy": "NaN"}, {"reporter_phone": ""}, {"reporter_name": ""}]:
            self.assertEqual(self.client.post("/api/v1/incident-reports/", {**body, **changes}, format="json").status_code, 400)

    def test_general_lists_are_masked_and_contact_endpoint_is_private(self):
        data = self.client.get(self.endpoint("")).data
        self.assertNotEqual(data["reporter_phone"], self.report.reporter_phone)
        response = self.client.get(self.endpoint())
        self.assertEqual(response.data["reporter_phone"], self.report.reporter_phone)
        self.assertTrue(response.data["can_call"])
        self.assertEqual(response["Cache-Control"], "no-store")
        for actor in (self.other, self.outsider):
            self.client.force_authenticate(actor)
            self.assertIn(self.client.get(self.endpoint()).status_code, (403, 404))
        self.client.force_authenticate(self.rescue)
        self.assertEqual(self.client.get(self.endpoint()).status_code, 200)
        self.assertEqual(self.client.get(self.endpoint("")).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/incident-reports/").status_code, 403)

    def test_no_consent_means_no_call_or_full_disclosure(self):
        self.report.allow_contact = False
        self.report.save()
        response = self.client.get(self.endpoint())
        self.assertFalse(response.data["can_call"])
        self.assertTrue(response.data["phone_masked"])
        self.assertNotEqual(response.data["reporter_phone"], self.report.reporter_phone)

    def test_admin_full_access_requires_explicit_audited_purpose(self):
        self.client.force_authenticate(self.admin)
        self.assertTrue(self.client.get(self.endpoint()).data["phone_masked"])
        self.assertFalse(ContactAccess.objects.exists())
        response = self.client.get(self.endpoint(), {"purpose": "Investigate synthetic data"})
        self.assertFalse(response.data["phone_masked"])
        access = ContactAccess.objects.get()
        self.assertEqual(access.actor, self.admin)
        self.assertEqual(access.report, self.report)

    def test_complete_lifecycle_timestamps_and_incident_metrics(self):
        for status in ("accepted", "en_route", "on_scene", "responding", "completed"):
            self.transition(status)
        self.assignment.refresh_from_db()
        keys = ("dispatched_at", "accepted_at", "en_route_at", "arrived_at", "responding_at", "completed_at")
        values = [getattr(self.assignment, key) for key in keys]
        self.assertTrue(all(values))
        self.assertEqual(values, sorted(values))
        self.assertEqual(self.assignment.completed_at, self.assignment.ended_at)
        before = self.client.get(f"/api/v1/incidents/{self.incident.pk}/timeline/").data
        self.assertIsNotNone(before["time_to_accept_seconds"])
        self.assertIsNone(before["time_to_resolve_seconds"])
        change_incident_status(actor=self.dispatcher, incident_id=self.incident.pk, expected_status="in_progress", status="resolved")
        data = self.client.get(f"/api/v1/incidents/{self.incident.pk}/timeline/").data
        self.assertIsNotNone(data["time_to_resolve_seconds"])
        self.assertEqual(data["milestones"]["report_created_at"], self.report.created_at)
        self.assertEqual(data["milestones"]["verified_at"], self.report.reviewed_at)
        self.assertEqual(data["milestones"]["completed_at"], self.assignment.completed_at)
        metrics = self.client.get("/api/v1/incidents/metrics/").data
        self.assertEqual(metrics["mtta_sample_count"], 1)
        self.assertEqual(metrics["mttr_sample_count"], 1)
        self.assertEqual(metrics["mtta_seconds"], before["time_to_accept_seconds"])
        self.assertEqual(metrics["mttr_seconds"], data["time_to_resolve_seconds"])

    def test_metrics_exclude_missing_milestones_and_guard_roles(self):
        response = self.client.get("/api/v1/incidents/metrics/")
        self.assertEqual(response.data["mttr_sample_count"], 0)
        self.assertIsNone(response.data["mttr_seconds"])
        self.assertIsNone(response.data["mtta_seconds"])
        for actor in (self.rescue, self.citizen):
            self.client.force_authenticate(actor)
            self.assertEqual(self.client.get("/api/v1/incidents/metrics/").status_code, 403)

    def test_responding_cannot_be_skipped_and_retry_preserves_timestamp(self):
        for status in ("accepted", "en_route", "on_scene"):
            self.transition(status)
        with self.assertRaises(Conflict):
            self.transition("completed")
        first = self.transition("responding")
        retried = self.transition("responding")
        self.assertEqual(first.responding_at, retried.responding_at)
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, "busy")
        with self.assertRaises(Conflict):
            change_incident_status(actor=self.dispatcher, incident_id=self.incident.pk, expected_status="in_progress", status="resolved")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Assignment.objects.create(incident=self.incident, team=self.team, assigned_by=self.dispatcher)

    def test_contact_and_source_access_revoked_after_completion(self):
        for status in ("accepted", "en_route", "on_scene", "responding", "completed"):
            self.transition(status)
        self.client.force_authenticate(self.rescue)
        self.assertEqual(self.client.get(self.endpoint()).status_code, 404)
        self.assertEqual(self.client.get(self.endpoint("")).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/assignments/{self.assignment.pk}/contacts/").status_code, 409)
        self.client.force_authenticate(self.dispatcher)
        change_incident_status(actor=self.dispatcher, incident_id=self.incident.pk, expected_status="in_progress", status="resolved")
        self.assertTrue(self.client.get(self.endpoint()).data["phone_masked"])

    def test_timestamp_audit_failure_rolls_back(self):
        self.transition("accepted")
        self.transition("en_route")
        with patch("dispatch.services.AssignmentHistory.objects.create", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.transition("on_scene")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, "en_route")
        self.assertIsNone(self.assignment.arrived_at)

    def test_signal_is_owned_idempotent_and_post_commit_without_pii(self):
        payload = self.signal()
        self.client.force_authenticate(self.rescue)
        with patch("realtime.events.deliver") as delivery, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/api/v1/assignments/{self.assignment.pk}/signals/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        event = delivery.call_args.args[1]
        self.assertEqual(event["type"], "assignment.signal_created")
        self.assertEqual(set(event["data"]), {"signal_id", "assignment_id", "incident_id", "team_id", "kind"})
        self.assertNotIn(self.report.reporter_phone, str(event))
        self.assertEqual(self.client.post(f"/api/v1/assignments/{self.assignment.pk}/signals/", payload, format="json").data["id"], response.data["id"])
        self.assertEqual(AssignmentSignal.objects.count(), 1)
        self.assertEqual(Assignment.objects.count(), 1)
        signal = AssignmentSignal.objects.get()
        self.assertEqual((signal.location.x, signal.location.y), (105, 21))
        self.assertEqual(self.client.post(f"/api/v1/assignments/{self.assignment.pk}/signals/", {**payload, "note": "Changed"}, format="json").status_code, 409)
        for actor, expected in ((self.dispatcher, 403), (self.citizen, 403), (self.outsider, 404)):
            self.client.force_authenticate(actor)
            self.assertEqual(self.client.post(f"/api/v1/assignments/{self.assignment.pk}/signals/", payload, format="json").status_code, expected)

    def test_signal_invalid_data_and_ended_mission_rejected(self):
        self.client.force_authenticate(self.rescue)
        url = f"/api/v1/assignments/{self.assignment.pk}/signals/"
        for changes in [{"latitude": 91}, {"longitude": "NaN"}, {"accuracy": -1}, {"support_type": "unknown"},
                        {"reported_at": (timezone.now() - timedelta(hours=1)).isoformat()}, {"team_id": self.team.pk}]:
            self.assertEqual(self.client.post(url, self.signal(**changes), format="json").status_code, 400, changes)
        problem = self.signal(kind="problem", problem="blocked_road")
        del problem["support_type"]
        self.assertEqual(self.client.post(url, problem, format="json").status_code, 201)
        for status in ("accepted", "en_route", "on_scene", "responding", "completed"):
            self.transition(status)
        self.assertEqual(self.client.post(url, self.signal(), format="json").status_code, 409)

    @override_settings(S3_BUCKET_NAME="synthetic-private-bucket")
    def test_camera_capture_metadata_and_linked_report_media_access(self):
        from evidence.services import create_upload, confirm_upload
        fresh = IncidentReport.objects.create(reporter=self.citizen, category=self.category, description="Synthetic", location=Point(105, 21, srid=4326))
        payload = {"report_id": fresh.pk, "filename": "scene.jpg", "content_type": "image/jpeg", "size_bytes": 3,
            "checksum_sha256": base64.b64encode(hashlib.sha256(b"abc").digest()).decode(),
            "capture_source": "camera", "captured_at": timezone.now().isoformat(),
            "capture_latitude": 21, "capture_longitude": 105, "capture_accuracy": 5}
        self.client.force_authenticate(self.citizen)
        with patch("evidence.s3.presign_upload", return_value={"url": "https://synthetic.invalid"}):
            response = self.client.post("/api/v1/media/presign/", payload, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            asset = MediaAsset.objects.get(pk=response.data["media"]["id"])
            self.assertEqual(asset.capture_source, "camera")
            self.assertEqual(asset.capture_longitude, 105)
            self.assertEqual(response.data["media"]["media_type"], "image")
            for changes in [{"capture_source": "upload", "captured_at": None}, {"captured_at": None}, {"capture_latitude": 91},
                            {"captured_at": (timezone.now() + timedelta(hours=1)).isoformat()}, {"object_key": "injected"}]:
                self.assertEqual(self.client.post("/api/v1/media/presign/", {**payload, **changes}, format="json").status_code, 400)
        fresh.incident = self.incident
        fresh.review_status = "accepted"
        fresh.save()
        with patch("evidence.s3.inspect_object", return_value={"ContentLength": 3, "ContentType": "image/jpeg", "ChecksumSHA256": asset.checksum_sha256, "Metadata": {"media-id": str(asset.pk)}}), patch("evidence.s3.mark_confirmed"):
            confirm_upload(actor=self.citizen, media_id=asset.pk)
        self.client.force_authenticate(self.rescue)
        media = self.client.get(f"/api/v1/media/?incident_id={self.incident.pk}")
        self.assertEqual(media.data["results"][0]["id"], str(asset.pk))
        self.assertEqual(self.client.get(f"/api/v1/media/?report_id={fresh.pk}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/media/{asset.pk}/").status_code, 404)
        for status in ("accepted", "en_route", "on_scene", "responding", "completed"):
            self.transition(status)
        self.assertEqual(self.client.get(f"/api/v1/media/?report_id={fresh.pk}").status_code, 404)

    def test_legacy_backfill_uses_history_and_does_not_invent_responding(self):
        self.transition("accepted")
        self.transition("en_route")
        self.transition("on_scene")
        Assignment.objects.filter(pk=self.assignment.pk).update(accepted_at=None, arrived_at=None)
        backfill = importlib.import_module("dispatch.migrations.0004_backfill_assignment_milestones").backfill
        with connection.schema_editor(atomic=False) as editor:
            backfill(apps, editor)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.arrived_at, self.assignment.history.get(to_status="on_scene").created_at)
        self.assertEqual(self.assignment.dispatched_at, self.assignment.created_at)
        self.assertIsNone(self.assignment.responding_at)

    def draft_payload(self, **overrides):
        return {"request_id": str(uuid4()), "category": self.category.pk, "description": "Synthetic draft",
            "latitude": 21, "longitude": 105, "reporter_name": "Synthetic draft reporter",
            "reporter_phone": "+12025550123", "allow_contact": True, **overrides}

    def test_draft_is_private_and_not_a_cluster_candidate(self):
        from incidents.clustering import candidate_queries
        self.client.force_authenticate(self.citizen)
        with patch("realtime.events.deliver") as delivery, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/v1/incident-reports/drafts/", self.draft_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["is_draft"])
        self.assertIsNone(response.data["reported_at"])
        draft = IncidentReport.objects.get(pk=response.data["id"])
        delivery.assert_not_called()
        unlinked = IncidentReport.objects.create(reporter=self.citizen, category=self.category, description="Synthetic source", location=Point(105, 21, srid=4326))
        self.assertNotIn(draft.pk, candidate_queries(unlinked)[0].values_list("pk", flat=True))
        for actor in (self.dispatcher, self.admin, self.other, self.rescue):
            self.client.force_authenticate(actor)
            self.assertEqual(self.client.get(f"/api/v1/incident-reports/{draft.pk}/").status_code, 404)
            self.assertEqual(self.client.get(f"/api/v1/incident-reports/{draft.pk}/contact/").status_code, 404)
            self.assertEqual(self.client.get(f"/api/v1/media/?report_id={draft.pk}").status_code, 404)
        self.client.force_authenticate(self.dispatcher)
        ids = [item["id"] for item in self.client.get("/api/v1/incident-reports/").data["results"]]
        self.assertNotIn(draft.pk, ids)

    def test_draft_request_retry_and_submission_are_idempotent(self):
        self.client.force_authenticate(self.citizen)
        body = self.draft_payload()
        first = self.client.post("/api/v1/incident-reports/drafts/", body, format="json")
        second = self.client.post("/api/v1/incident-reports/drafts/", {**body, "description": "Updated synthetic draft"}, format="json")
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(second.data["description"], "Updated synthetic draft")
        pk = first.data["id"]
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post("/api/v1/incident-reports/drafts/", body, format="json").status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/incident-reports/{pk}/submit/", {}, format="json").status_code, 404)
        self.client.force_authenticate(self.citizen)
        with patch("realtime.events.deliver") as delivery, self.captureOnCommitCallbacks(execute=True):
            sent = self.client.post(f"/api/v1/incident-reports/{pk}/submit/", {}, format="json")
        self.assertFalse(sent.data["is_draft"])
        self.assertIsNotNone(sent.data["reported_at"])
        self.assertEqual(delivery.call_args.args[1]["type"], "report.changed")
        again = self.client.post(f"/api/v1/incident-reports/{pk}/submit/", {}, format="json")
        self.assertEqual(again.data["reported_at"], sent.data["reported_at"])
        recovered = self.client.post("/api/v1/incident-reports/drafts/", {**body, "description": "Attempt rewrite after submission"}, format="json")
        self.assertFalse(recovered.data["is_draft"])
        self.assertEqual(recovered.data["description"], "Updated synthetic draft")

    @override_settings(S3_BUCKET_NAME="synthetic-private-bucket")
    def test_draft_cannot_publish_unconfirmed_image_and_confirmation_does_not_broadcast(self):
        from evidence.services import create_upload, confirm_upload
        self.client.force_authenticate(self.citizen)
        draft = self.client.post("/api/v1/incident-reports/drafts/", self.draft_payload(), format="json").data
        payload = {"report_id": draft["id"], "filename": "synthetic.jpg", "content_type": "image/jpeg", "size_bytes": 3,
            "checksum_sha256": base64.b64encode(hashlib.sha256(b"abc").digest()).decode(),
            "capture_source": "camera", "captured_at": timezone.now().isoformat()}
        with patch("evidence.s3.presign_upload", return_value={}):
            asset, _ = create_upload(actor=self.citizen, data=payload)
        self.assertEqual(self.client.post(f"/api/v1/incident-reports/{draft['id']}/submit/", {}, format="json").status_code, 409)
        with patch("evidence.s3.inspect_object", return_value={"ContentLength": 3, "ContentType": "image/jpeg", "ChecksumSHA256": asset.checksum_sha256, "Metadata": {"media-id": str(asset.pk)}}), patch("evidence.s3.mark_confirmed"), patch("realtime.events.deliver") as delivery, self.captureOnCommitCallbacks(execute=True):
            confirm_upload(actor=self.citizen, media_id=asset.pk)
        delivery.assert_not_called()
        sent = self.client.post(f"/api/v1/incident-reports/{draft['id']}/submit/", {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        self.client.force_authenticate(self.dispatcher)
        self.assertEqual(self.client.get(f"/api/v1/media/?report_id={draft['id']}").data["results"][0]["status"], "ready")
