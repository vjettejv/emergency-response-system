from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase

from accounts.models import Role, User
from incidents import services
from incidents.models import Incident, IncidentCategory, IncidentReport, IncidentStatus, IncidentStatusHistory


class IncidentAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.citizen = User.objects.create_user(username="reporter", role=Role.CITIZEN)
        cls.other = User.objects.create_user(username="other", role=Role.CITIZEN)
        cls.dispatcher = User.objects.create_user(username="dispatcher", role=Role.DISPATCHER)
        cls.admin = User.objects.create_user(username="admin", role=Role.ADMIN)
        cls.rescue = User.objects.create_user(username="rescue", role=Role.RESCUE_TEAM)
        cls.category = IncidentCategory.objects.create(code="synthetic-fire", name="Synthetic fire")
        cls.other_category = IncidentCategory.objects.create(code="synthetic-medical", name="Synthetic medical")
        cls.inactive_category = IncidentCategory.objects.create(code="inactive", name="Inactive", is_active=False)

    def setUp(self):
        self.client.force_authenticate(self.citizen)

    def report(self, **overrides):
        data = {
            "reporter": self.citizen, "category": self.category,
            "description": "Synthetic smoke report", "location": Point(105.8, 21.0, srid=4326),
        }
        data.update(overrides)
        return IncidentReport.objects.create(**data)

    def incident(self, **overrides):
        data = {
            "title": "Synthetic incident", "category": self.category,
            "location": Point(105.8, 21.0, srid=4326), "status": IncidentStatus.VERIFIED,
        }
        data.update(overrides)
        return Incident.objects.create(**data)

    def report_payload(self, **overrides):
        data = {
            "category": self.category.pk, "description": "Synthetic smoke report",
            "latitude": 21.01, "longitude": 105.81,
            "reporter_name": "Synthetic Citizen", "reporter_phone": "+12025550123",
        }
        data.update(overrides)
        return data

    def verify(self, report, decision="accepted", **extra):
        return self.client.post(reverse("incidents:report-verify", args=[report.pk]), {
            "review_status": decision, **extra,
        }, format="json")

    def create_from(self, report):
        return self.client.post(reverse("incidents:report-create-incident", args=[report.pk]), {
            "title": "Synthetic confirmed incident",
        }, format="json")

    def link(self, incident, ids):
        return self.client.post(reverse("incidents:incident-reports", args=[incident.pk]), {
            "report_ids": ids,
        }, format="json")

    def change_status(self, incident, status, expected="verified", **extra):
        return self.client.patch(reverse("incidents:incident-change-status", args=[incident.pk]), {
            "status": status, "expected_status": expected, **extra,
        }, format="json")

    def test_submission_stores_longitude_first_and_server_timestamp(self):
        before = timezone.now()
        occurred = (before - timedelta(minutes=10)).isoformat()
        media = [{"filename": "synthetic.jpg", "content_type": "image/jpeg", "size_bytes": 12345}]
        response = self.client.post(reverse("incidents:report-list"), self.report_payload(
            occurred_at=occurred, media_metadata=media,
        ), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        report = IncidentReport.objects.get(pk=response.data["id"])
        self.assertEqual(report.reporter, self.citizen)
        self.assertEqual(report.review_status, "pending")
        self.assertIsNone(report.incident_id)
        self.assertIsNone(report.reviewed_at)
        self.assertEqual(report.location.srid, 4326)
        self.assertEqual((report.location.x, report.location.y), (105.81, 21.01))
        self.assertEqual(response.data["longitude"], 105.81)
        self.assertEqual(response.data["latitude"], 21.01)
        self.assertEqual(report.media_metadata, media)
        self.assertGreaterEqual(report.created_at, before)
        self.assertIn("reported_at", response.data)

    def test_invalid_coordinates_rejected_without_writing(self):
        cases = [
            {"latitude": 91}, {"latitude": -91}, {"longitude": 181}, {"longitude": -181},
            {"latitude": "NaN"}, {"longitude": "Infinity"}, {"latitude": True},
            {"longitude": None}, {"latitude": "invalid"},
        ]
        for case in cases:
            with self.subTest(case=case):
                response = self.client.post(reverse("incidents:report-list"), self.report_payload(**case), format="json")
                self.assertEqual(response.status_code, 400)
        self.assertFalse(IncidentReport.objects.exists())

    def test_coordinate_boundaries_and_zero_are_valid(self):
        for latitude, longitude in ((0, 0), (-90, -180), (90, 180)):
            response = self.client.post(reverse("incidents:report-list"), self.report_payload(
                latitude=latitude, longitude=longitude,
            ), format="json")
            self.assertEqual(response.status_code, 201, response.data)

    def test_inactive_missing_category_and_empty_description_rejected(self):
        for case in (
            {"category": self.inactive_category.pk}, {"category": 999999}, {"description": "  "},
        ):
            response = self.client.post(reverse("incidents:report-list"), self.report_payload(**case), format="json")
            self.assertEqual(response.status_code, 400)
        self.assertFalse(IncidentReport.objects.exists())

    def test_media_schema_and_limits(self):
        good = {"filename": "synthetic.jpg", "content_type": "image/jpeg", "size_bytes": 12}
        for metadata in (
            {"filename": "not-a-list"}, [good] * 11,
            [{**good, "size_bytes": -1}], [{**good, "size_bytes": 50 * 1024 * 1024 + 1}],
            [{**good, "content_type": "text/html"}], [{**good, "url": "https://example.com/file"}],
        ):
            response = self.client.post(reverse("incidents:report-list"), self.report_payload(
                media_metadata=metadata,
            ), format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(IncidentReport.objects.exists())

    def test_time_validation_and_server_field_protection(self):
        cases = [
            {"occurred_at": (timezone.now() + timedelta(days=1)).isoformat()},
            {"occurred_at": "2026-01-01T10:00:00"},
            {"occurred_at": "invalid"},
            {"reported_at": "2026-01-01T10:00:00Z"},
            {"reporter": self.other.pk}, {"review_status": "accepted"}, {"incident": 1},
        ]
        for case in cases:
            response = self.client.post(reverse("incidents:report-list"), self.report_payload(**case), format="json")
            self.assertEqual(response.status_code, 400, response.data)

    def test_citizen_can_only_read_own_reports(self):
        own = self.report()
        other = self.report(reporter=self.other)
        response = self.client.get(reverse("incidents:report-list"))
        self.assertEqual([item["id"] for item in response.data["results"]], [own.pk])
        self.assertEqual(self.client.get(reverse("incidents:report-detail", args=[own.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("incidents:report-detail", args=[other.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("incidents:incident-list")).status_code, 403)

    def test_anonymous_cannot_access_resources(self):
        self.client.force_authenticate(None)
        for name in ("report-list", "incident-list", "category-list"):
            self.assertEqual(self.client.get(reverse(f"incidents:{name}")).status_code, 401)
        self.assertEqual(self.client.post(reverse("incidents:report-list"), self.report_payload()).status_code, 401)

    def test_active_categories_are_available_to_all_authenticated_roles(self):
        for actor in (self.citizen, self.dispatcher, self.admin, self.rescue):
            self.client.force_authenticate(actor)
            response = self.client.get(reverse("incidents:category-list"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["count"], 2)

    def test_role_matrix_for_mutating_endpoints(self):
        report = self.report()
        incident = self.incident()
        for actor in (self.citizen, self.rescue):
            self.client.force_authenticate(actor)
            self.assertEqual(self.verify(report).status_code, 403)
            self.assertEqual(self.create_from(report).status_code, 403)
            self.assertEqual(self.link(incident, [report.pk]).status_code, 403)
            self.assertEqual(self.change_status(incident, "in_progress").status_code, 403)
        for actor in (self.dispatcher, self.admin, self.rescue):
            self.client.force_authenticate(actor)
            response = self.client.post(reverse("incidents:report-list"), self.report_payload(), format="json")
            self.assertEqual(response.status_code, 403)
        self.client.force_authenticate(self.rescue)
        self.assertEqual(self.client.get(reverse("incidents:report-list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("incidents:incident-list")).status_code, 403)

    def test_admin_can_review_create_link_and_update(self):
        self.client.force_authenticate(self.admin)
        report = self.report()
        self.assertEqual(self.verify(report).status_code, 200)
        response = self.create_from(report)
        self.assertEqual(response.status_code, 201)
        incident = Incident.objects.get(pk=response.data["id"])
        additional = self.report(review_status="accepted")
        self.assertEqual(self.link(incident, [additional.pk]).status_code, 200)
        self.assertEqual(self.change_status(incident, "in_progress").status_code, 200)

    def test_review_audit_is_immutable_and_repeat_is_noop(self):
        self.client.force_authenticate(self.dispatcher)
        report = self.report()
        self.assertEqual(self.verify(report, note="Verified by phone").status_code, 200)
        report.refresh_from_db()
        reviewed_at = report.reviewed_at
        self.assertEqual(report.reviewed_by, self.dispatcher)
        self.assertEqual(self.verify(report, note="Should not overwrite").status_code, 200)
        report.refresh_from_db()
        self.assertEqual(report.reviewed_at, reviewed_at)
        self.assertEqual(report.review_note, "Verified by phone")
        self.assertEqual(self.verify(report, "rejected").status_code, 409)
        self.assertEqual(self.verify(report, "pending").status_code, 400)

    def test_rejected_and_pending_reports_cannot_create_incident(self):
        self.client.force_authenticate(self.dispatcher)
        pending = self.report()
        rejected = self.report()
        self.assertEqual(self.verify(rejected, "rejected").status_code, 200)
        for report in (pending, rejected):
            self.assertEqual(self.create_from(report).status_code, 409)
        self.assertFalse(Incident.objects.exists())

    def test_create_incident_copies_report_and_records_initial_history(self):
        self.client.force_authenticate(self.dispatcher)
        report = self.report(review_status="accepted", address="Synthetic street")
        response = self.create_from(report)
        self.assertEqual(response.status_code, 201, response.data)
        incident = Incident.objects.get(pk=response.data["id"])
        report.refresh_from_db()
        self.assertEqual(report.incident, incident)
        self.assertEqual(incident.status, IncidentStatus.VERIFIED)
        self.assertEqual(incident.location, report.location)
        self.assertEqual(incident.description, report.description)
        self.assertEqual(incident.category, report.category)
        self.assertEqual(incident.address, report.address)
        self.assertEqual(incident.created_by, self.dispatcher)
        history = incident.status_history.get()
        self.assertEqual((history.from_status, history.to_status), ("", "verified"))
        self.assertEqual(history.changed_by, self.dispatcher)
        self.assertEqual(self.create_from(report).status_code, 409)
        self.assertEqual(Incident.objects.count(), 1)

    def test_batch_link_and_repeated_link_preserve_original_reports(self):
        self.client.force_authenticate(self.dispatcher)
        incident = self.incident()
        reports = [self.report(review_status="accepted") for _ in range(2)]
        for _ in range(2):
            self.assertEqual(self.link(incident, [r.pk for r in reports]).status_code, 200)
        self.assertEqual(incident.reports.count(), 2)
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentReport.objects.count(), 2)
        response = self.client.get(reverse("incidents:incident-reports", args=[incident.pk]))
        self.assertEqual(response.data["count"], 2)

    def test_batch_link_failure_rolls_back_every_report(self):
        self.client.force_authenticate(self.dispatcher)
        incident = self.incident()
        good = self.report(review_status="accepted")
        invalid_reports = [
            self.report(), self.report(review_status="rejected"),
            self.report(review_status="accepted", category=self.other_category),
            self.report(review_status="accepted", incident=self.incident()),
        ]
        for invalid in invalid_reports:
            response = self.link(incident, [good.pk, invalid.pk])
            self.assertEqual(response.status_code, 409, response.data)
            good.refresh_from_db()
            self.assertIsNone(good.incident_id)
        self.assertEqual(self.link(incident, [good.pk, 999999]).status_code, 404)
        good.refresh_from_db()
        self.assertIsNone(good.incident_id)

    def test_closed_incident_and_invalid_batch_are_rejected(self):
        self.client.force_authenticate(self.dispatcher)
        incident = self.incident()
        report = self.report(review_status="accepted")
        for ids in ([], [report.pk, report.pk], [0], list(range(1, 102))):
            self.assertEqual(self.link(incident, ids).status_code, 400)
        incident.status = IncidentStatus.RESOLVED
        incident.save()
        self.assertEqual(self.link(incident, [report.pk]).status_code, 409)

    def test_status_transitions_history_and_noop(self):
        self.client.force_authenticate(self.dispatcher)
        incident = self.incident()
        self.assertEqual(self.change_status(incident, "in_progress", note="Confirmed work started").status_code, 200)
        self.assertEqual(self.change_status(incident, "in_progress", expected="in_progress").status_code, 200)
        self.assertEqual(self.change_status(incident, "resolved", expected="in_progress").status_code, 200)
        self.assertEqual(incident.status_history.count(), 2)
        history = incident.status_history.first()
        self.assertEqual(history.changed_by, self.dispatcher)
        self.assertEqual(history.note, "Confirmed work started")
        self.assertEqual(self.change_status(incident, "verified", expected="resolved").status_code, 409)
        response = self.client.get(reverse("incidents:incident-status-history", args=[incident.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)

    def test_stale_invalid_and_dispatch_status_updates_are_rejected(self):
        self.client.force_authenticate(self.dispatcher)
        incident = self.incident()
        self.assertEqual(self.change_status(incident, "cancelled", expected="new").status_code, 409)
        self.assertEqual(self.change_status(incident, "dispatched").status_code, 409)
        self.assertEqual(self.change_status(incident, "resolved").status_code, 409)
        self.assertEqual(self.change_status(incident, "invalid").status_code, 400)
        url = reverse("incidents:incident-change-status", args=[incident.pk])
        self.assertEqual(self.client.patch(url, {"status": "cancelled"}, format="json").status_code, 400)
        incident.refresh_from_db()
        self.assertEqual(incident.status, "verified")
        self.assertFalse(incident.status_history.exists())

    def test_new_incident_can_be_verified_or_cancelled(self):
        self.client.force_authenticate(self.dispatcher)
        for target in ("verified", "cancelled"):
            incident = self.incident(status="new")
            self.assertEqual(self.change_status(incident, target, expected="new").status_code, 200)

    def test_history_failure_rolls_back_incident_creation_and_report_link(self):
        report = self.report(review_status="accepted")
        with patch("incidents.services.IncidentStatusHistory.objects.create", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                services.create_incident_from_report(actor=self.dispatcher, report_id=report.pk, title="Synthetic")
        report.refresh_from_db()
        self.assertIsNone(report.incident_id)
        self.assertFalse(Incident.objects.exists())

    def test_history_failure_rolls_back_status_change(self):
        incident = self.incident()
        with patch("incidents.services.IncidentStatusHistory.objects.create", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                services.change_incident_status(
                    actor=self.dispatcher, incident_id=incident.pk, status="in_progress", expected_status="verified",
                )
        incident.refresh_from_db()
        self.assertEqual(incident.status, "verified")

    def test_services_enforce_roles_without_api_layer(self):
        report = self.report(review_status="accepted")
        incident = self.incident()
        calls = [
            lambda: services.review_report(actor=self.citizen, report_id=report.pk, review_status="accepted"),
            lambda: services.create_incident_from_report(actor=self.citizen, report_id=report.pk, title="Bad"),
            lambda: services.link_reports(actor=self.rescue, incident_id=incident.pk, report_ids=[report.pk]),
            lambda: services.change_incident_status(
                actor=self.citizen, incident_id=incident.pk, status="in_progress", expected_status="verified",
            ),
            lambda: services.submit_report(
                actor=self.dispatcher, category=self.category, description="Bad", location=Point(105, 21, srid=4326),
            ),
        ]
        for call in calls:
            with self.assertRaises(PermissionDenied):
                call()

    def test_report_filters_and_search_combine(self):
        self.client.force_authenticate(self.dispatcher)
        target = self.report(review_status="accepted", description="Synthetic smoke alpha")
        self.report(review_status="pending")
        self.report(review_status="accepted", category=self.other_category)
        old = self.report(review_status="accepted")
        IncidentReport.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
        response = self.client.get(reverse("incidents:report-list"), {
            "status": "accepted", "category": self.category.pk, "search": "alpha",
            "created_after": (timezone.now() - timedelta(days=1)).isoformat(),
            "created_before": (timezone.now() + timedelta(days=1)).isoformat(),
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([r["id"] for r in response.data["results"]], [target.pk])

    def test_incident_filters_search_and_pagination(self):
        self.client.force_authenticate(self.dispatcher)
        target = self.incident(title="Synthetic target")
        self.incident(title="Synthetic target", status="cancelled")
        self.incident(category=self.other_category)
        response = self.client.get(reverse("incidents:incident-list"), {
            "status": "verified", "category": self.category.pk, "search": "target", "page_size": 1,
            "created_after": (timezone.now() - timedelta(days=1)).isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], target.pk)
        response = self.client.get(reverse("incidents:incident-list"), {"page_size": 1})
        self.assertEqual(len(response.data["results"]), 1)
        self.assertIsNotNone(response.data["next"])

    def test_invalid_filters_return_400(self):
        self.client.force_authenticate(self.dispatcher)
        queries = [
            {"status": "bad"}, {"category": "bad"}, {"created_after": "bad"},
            {"created_before": "2026-01-01T00:00:00"},
            {"created_after": "2026-02-01T00:00:00Z", "created_before": "2026-01-01T00:00:00Z"},
        ]
        for name in ("report-list", "incident-list"):
            for query in queries:
                response = self.client.get(reverse(f"incidents:{name}"), query)
                self.assertEqual(response.status_code, 400, response.data)

    def test_detail_does_not_apply_collection_filter(self):
        self.client.force_authenticate(self.dispatcher)
        report = self.report()
        response = self.client.get(reverse("incidents:report-detail", args=[report.pk]), {"status": "accepted"})
        self.assertEqual(response.status_code, 200)

    def test_missing_resources_and_unexposed_mutations(self):
        self.client.force_authenticate(self.dispatcher)
        self.assertEqual(self.client.get(reverse("incidents:report-detail", args=[999999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("incidents:incident-detail", args=[999999])).status_code, 404)
        incident = self.incident()
        detail = reverse("incidents:incident-detail", args=[incident.pk])
        self.assertEqual(self.client.delete(detail).status_code, 405)
        self.assertEqual(self.client.patch(detail, {"status": "resolved"}).status_code, 405)
        self.assertEqual(self.client.post(reverse("incidents:incident-list"), {}).status_code, 405)

    def test_token_authentication_end_to_end(self):
        token = Token.objects.create(user=self.citizen)
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = self.client.post(reverse("incidents:report-list"), self.report_payload(), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["reporter"], self.citizen.pk)
