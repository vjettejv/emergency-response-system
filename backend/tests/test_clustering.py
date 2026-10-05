import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase

from accounts.models import Role, User
from incidents.clustering import candidate_queries, dismiss_potential_duplicate, find_potential_duplicates
from incidents.models import DuplicateDismissal, Incident, IncidentCategory, IncidentReport, IncidentReportLinkHistory
from incidents.services import create_incident_from_report, link_reports


@override_settings(CLUSTER_RADIUS_METERS=300, CLUSTER_TIME_WINDOW_MINUTES=30, CLUSTER_MAX_CANDIDATES=50)
class ClusteringTests(APITestCase):
    def setUp(self):
        self.citizen = User.objects.create_user(username="citizen")
        self.dispatcher = User.objects.create_user(username="dispatcher", role=Role.DISPATCHER)
        self.admin = User.objects.create_user(username="admin", role=Role.ADMIN)
        self.rescue = User.objects.create_user(username="rescue", role=Role.RESCUE_TEAM)
        self.category = IncidentCategory.objects.create(code="synthetic", name="Synthetic")
        self.now = timezone.now()
        self.source = self.report()
        self.client.force_authenticate(self.dispatcher)

    def report(self, **overrides):
        received_at = overrides.pop("received_at", self.now)
        data = {
            "reporter": self.citizen, "category": self.category, "description": "Synthetic report",
            "location": Point(105, 21, srid=4326),
        }
        data.update(overrides)
        report = IncidentReport.objects.create(**data)
        IncidentReport.objects.filter(pk=report.pk).update(created_at=received_at)
        report.created_at = received_at
        return report

    def incident(self, **overrides):
        created_at = overrides.pop("created_at", self.now)
        data = {"title": "Synthetic", "category": self.category, "location": Point(105, 21, srid=4326), "status": "verified"}
        data.update(overrides)
        incident = Incident.objects.create(**data)
        Incident.objects.filter(pk=incident.pk).update(created_at=created_at)
        return incident

    def find(self, report=None):
        return find_potential_duplicates(actor=self.dispatcher, report_id=(report or self.source).pk)

    def report_ids(self, result=None):
        return [item.pk for item in (result or self.find())["reports"]]

    def test_same_location_and_near_time_are_candidates(self):
        pending = self.report(received_at=self.now - timedelta(minutes=5))
        accepted = self.report(review_status="accepted")
        result = self.find()
        self.assertCountEqual(self.report_ids(result), [pending.pk, accepted.pk])
        self.assertTrue(all(item.distance_meters == 0 for item in result["reports"]))
        self.assertNotIn(self.source.pk, self.report_ids(result))

    def test_geography_uses_meters_and_excludes_outside_radius(self):
        inside = self.report(location=Point(105.001, 21, srid=4326))
        self.report(location=Point(105.02, 21, srid=4326))
        result = self.find()
        self.assertEqual(self.report_ids(result), [inside.pk])
        self.assertGreater(result["reports"][0].distance_meters, 100)
        self.assertLess(result["reports"][0].distance_meters, 110)

    def test_dwithin_radius_boundary_using_postgis_projection(self):
        points = []
        with connection.cursor() as cursor:
            for meters in (299.9, 300.1):
                cursor.execute(
                    "SELECT ST_AsText(ST_Project(ST_SetSRID(ST_MakePoint(105, 21), 4326)::geography, %s, 0)::geometry)",
                    [meters],
                )
                points.append(Point.from_ewkt("SRID=4326;" + cursor.fetchone()[0]))
        inside = self.report(location=points[0])
        self.report(location=points[1])
        self.assertEqual(self.report_ids(), [inside.pk])

    def test_time_window_is_inclusive_and_two_sided(self):
        lower = self.report(received_at=self.now - timedelta(minutes=30))
        upper = self.report(received_at=self.now + timedelta(minutes=30))
        self.report(received_at=self.now - timedelta(minutes=30, seconds=1))
        self.report(received_at=self.now + timedelta(minutes=30, seconds=1))
        self.assertCountEqual(self.report_ids(), [lower.pk, upper.pk])

    def test_occurrence_time_takes_precedence_over_receipt_time(self):
        self.source.occurred_at = self.now - timedelta(days=2)
        self.source.save(update_fields=["occurred_at"])
        matching = self.report(occurred_at=self.source.occurred_at + timedelta(minutes=5))
        fallback = self.report(received_at=self.source.occurred_at)
        self.report()  # Newly received, but no matching occurrence time.
        self.assertCountEqual(self.report_ids(), [matching.pk, fallback.pk])

    def test_other_category_and_rejected_reports_are_excluded(self):
        category = IncidentCategory.objects.create(code="other", name="Other")
        self.report(category=category)
        self.report(review_status="rejected")
        self.assertEqual(self.report_ids(), [])

    def test_rejected_or_already_linked_source_has_no_suggestions(self):
        self.report()
        self.source.review_status = "rejected"
        self.source.save()
        self.assertEqual(self.find()["reports"], [])
        self.source.review_status = "accepted"
        self.source.incident = self.incident()
        self.source.save()
        result = self.find()
        self.assertEqual(result["reports"], [])
        self.assertEqual(result["incidents"], [])

    def test_linked_candidate_identifies_existing_incident_once(self):
        incident = self.incident()
        witness = self.report(incident=incident, review_status="accepted")
        self.report(incident=incident, review_status="accepted")
        result = self.find()
        self.assertIn(witness.pk, self.report_ids(result))
        self.assertEqual([item.pk for item in result["incidents"]], [incident.pk])
        self.assertEqual(result["incidents"][0].matched_report_id, witness.pk)

    def test_matching_report_can_witness_incident_with_old_time_and_far_point(self):
        incident = self.incident(location=Point(106, 21, srid=4326), created_at=self.now - timedelta(days=2))
        witness = self.report(incident=incident, review_status="accepted")
        result = self.find()
        self.assertEqual([item.pk for item in result["incidents"]], [incident.pk])
        self.assertEqual(result["incidents"][0].matched_report_id, witness.pk)
        self.assertEqual(result["incidents"][0].distance_meters, 0)

    def test_incident_direct_match_and_closed_far_or_old_incidents(self):
        direct = self.incident()
        self.incident(location=Point(106, 21, srid=4326))
        self.incident(created_at=self.now - timedelta(hours=1))
        closed = self.incident(status="resolved")
        self.report(incident=closed, review_status="accepted")
        result = self.find()
        self.assertEqual([item.pk for item in result["incidents"]], [direct.pk])
        self.assertIsNone(result["incidents"][0].matched_report_id)
        self.assertEqual(result["reports"], [])

    def test_config_changes_radius_and_window(self):
        candidate = self.report(location=Point(105.001, 21, srid=4326), received_at=self.now - timedelta(minutes=10))
        with override_settings(CLUSTER_RADIUS_METERS=50):
            self.assertEqual(self.report_ids(), [])
        with override_settings(CLUSTER_TIME_WINDOW_MINUTES=5):
            self.assertEqual(self.report_ids(), [])
        self.assertEqual(self.report_ids(), [candidate.pk])

    def test_results_are_bounded_ordered_and_queries_do_not_scale_per_candidate(self):
        candidates = [self.report() for _ in range(5)]
        with override_settings(CLUSTER_MAX_CANDIDATES=2):
            with self.assertNumQueries(3):
                result = self.find()
        self.assertEqual(self.report_ids(result), [r.pk for r in candidates[:2]])
        self.assertTrue(result["more_reports"])
        self.assertFalse(result["more_incidents"])

    def test_query_uses_dwithin_and_existing_gist_index_is_usable(self):
        self.report()
        # Spatially dispersed rows make the radius selective, unlike a two-row table.
        IncidentReport.objects.bulk_create([
            IncidentReport(
                reporter=self.citizen, category=self.category, description="Synthetic distant report",
                location=Point(106 + i * 0.001, 21, srid=4326), occurred_at=self.now,
            ) for i in range(500)
        ])
        reports, _ = candidate_queries(self.source)
        sql, _ = reports.query.sql_with_params()
        self.assertIn("ST_DWithin", sql)
        self.assertIn("ST_Distance", sql)
        self.assertIn("COALESCE", sql)
        with transaction.atomic(), connection.cursor() as cursor:
            # Check index eligibility on synthetic data, not production performance.
            cursor.execute("ANALYZE incidents_incidentreport")
            cursor.execute("SET LOCAL enable_seqscan = off")
            plan = json.loads(reports.explain(format="json"))
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = %s AND indexdef ILIKE %s",
                [IncidentReport._meta.db_table, "%USING gist (location)%"],
            )
            gist_indexes = [row[0] for row in cursor.fetchall()]
        self.assertTrue(gist_indexes)
        self.assertTrue(any(index in json.dumps(plan) for index in gist_indexes), plan)

    def test_submission_returns_private_hint_without_automatic_link(self):
        self.incident()
        self.client.force_authenticate(self.citizen)
        response = self.client.post(reverse("incidents:report-list"), {
            "category": self.category.pk, "description": "Synthetic new report", "latitude": 21, "longitude": 105,
            "reporter_name": "Synthetic Citizen", "reporter_phone": "+12025550123",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["potential_duplicates"], {
            "has_candidates": True, "dispatcher_confirmation_required": True,
        })
        self.assertIsNone(IncidentReport.objects.get(pk=response.data["id"]).incident_id)
        self.assertEqual(Incident.objects.count(), 1)
        self.assertFalse(IncidentReportLinkHistory.objects.exists())

    def test_dispatcher_api_returns_candidates_but_citizen_and_rescue_are_forbidden(self):
        candidate = self.report()
        url = reverse("incidents:report-potential-duplicates", args=[self.source.pk])
        for actor in (self.dispatcher, self.admin):
            self.client.force_authenticate(actor)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["reports"][0]["id"], candidate.pk)
            self.assertEqual(response.data["radius_meters"], 300)
        for actor in (self.citizen, self.rescue):
            self.client.force_authenticate(actor)
            self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_dismiss_pair_is_symmetric_persistent_and_idempotent(self):
        candidate = self.report()
        dismissal = dismiss_potential_duplicate(
            actor=self.dispatcher, report_id=candidate.pk, candidate_report_id=self.source.pk, reason="Separate events",
        )
        self.assertEqual(dismissal.report_id, self.source.pk)
        self.assertEqual(self.report_ids(), [])
        self.assertEqual(self.find(candidate)["reports"], [])
        again = dismiss_potential_duplicate(
            actor=self.admin, report_id=self.source.pk, candidate_report_id=candidate.pk, reason="Retry",
        )
        self.assertEqual(again.pk, dismissal.pk)
        self.assertEqual(again.reason, "Separate events")
        self.assertEqual(again.dismissed_by, self.dispatcher)

    def test_dismiss_incident_also_hides_its_linked_reports(self):
        incident = self.incident()
        self.report(incident=incident, review_status="accepted")
        dismiss_potential_duplicate(actor=self.dispatcher, report_id=self.source.pk, incident_id=incident.pk)
        result = self.find()
        self.assertEqual(result["reports"], [])
        self.assertEqual(result["incidents"], [])

    def test_dismissal_api_and_invalid_or_stale_targets(self):
        candidate = self.report()
        far = self.report(location=Point(106, 21, srid=4326))
        url = reverse("incidents:report-dismiss-duplicate", args=[self.source.pk])
        for body, code in (
            ({}, 400), ({"candidate_report_id": candidate.pk, "incident_id": 1}, 400),
            ({"candidate_report_id": self.source.pk}, 400), ({"candidate_report_id": far.pk}, 409),
            ({"candidate_report_id": 999999}, 404),
        ):
            self.assertEqual(self.client.post(url, body, format="json").status_code, code)
        response = self.client.post(url, {"candidate_report_id": candidate.pk, "reason": "Not the same event"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["dismissed_by"], self.dispatcher.pk)
        self.assertEqual(DuplicateDismissal.objects.count(), 1)

    def test_dismissal_permissions_enforced_in_api_and_service(self):
        candidate = self.report()
        url = reverse("incidents:report-dismiss-duplicate", args=[self.source.pk])
        for actor in (self.citizen, self.rescue):
            self.client.force_authenticate(actor)
            self.assertEqual(self.client.post(url, {"candidate_report_id": candidate.pk}).status_code, 403)
            with self.assertRaises(PermissionDenied):
                dismiss_potential_duplicate(actor=actor, report_id=self.source.pk, candidate_report_id=candidate.pk)
            with self.assertRaises(PermissionDenied):
                find_potential_duplicates(actor=actor, report_id=self.source.pk)

    def test_link_to_existing_incident_reuses_service_and_records_history_once(self):
        incident = self.incident()
        self.source.review_status = "accepted"
        self.source.save()
        url = reverse("incidents:incident-reports", args=[incident.pk])
        for _ in range(2):
            response = self.client.post(url, {"report_ids": [self.source.pk], "note": "Confirmed duplicate"}, format="json")
            self.assertEqual(response.status_code, 200)
        self.assertEqual(Incident.objects.count(), 1)
        history = incident.report_link_history.get()
        self.assertEqual(history.operation, "link")
        self.assertEqual(history.actor, self.dispatcher)
        self.assertEqual(history.note, "Confirmed duplicate")
        self.assertEqual(self.find()["reports"], [])
        create_url = reverse("incidents:report-create-incident", args=[self.source.pk])
        self.assertEqual(self.client.post(create_url, {"title": "No duplicate incident"}).status_code, 409)

    def test_merge_batch_preserves_reports_and_audits_changed_links(self):
        incident = self.incident()
        reports = [self.report(review_status="accepted") for _ in range(2)]
        link_reports(actor=self.dispatcher, incident_id=incident.pk, report_ids=[r.pk for r in reports], note="Confirmed")
        self.assertEqual(incident.report_link_history.count(), 2)
        self.assertEqual(set(incident.report_link_history.values_list("operation", flat=True)), {"merge"})
        self.assertEqual(incident.reports.count(), 2)
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentReport.objects.count(), 3)
        url = reverse("incidents:incident-report-link-history", args=[incident.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.client.force_authenticate(self.citizen)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_link_audit_failure_rolls_back_links(self):
        incident = self.incident()
        report = self.report(review_status="accepted")
        with patch("incidents.services.IncidentReportLinkHistory.objects.bulk_create", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                link_reports(actor=self.dispatcher, incident_id=incident.pk, report_ids=[report.pk])
        report.refresh_from_db()
        self.assertIsNone(report.incident_id)

    def test_incident_creation_records_link_audit(self):
        report = self.report(review_status="accepted")
        incident = create_incident_from_report(actor=self.dispatcher, report_id=report.pk, title="Synthetic")
        self.assertEqual(incident.report_link_history.get().operation, "create")
        self.assertEqual(incident.status_history.count(), 1)

    def test_create_link_audit_failure_rolls_back_incident_and_status_history(self):
        report = self.report(review_status="accepted")
        with patch("incidents.services.IncidentReportLinkHistory.objects.create", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                create_incident_from_report(actor=self.dispatcher, report_id=report.pk, title="Synthetic")
        report.refresh_from_db()
        self.assertIsNone(report.incident_id)
        self.assertFalse(Incident.objects.exists())

    def test_dismissal_does_not_prevent_explicit_manual_confirmation(self):
        incident = self.incident()
        dismiss_potential_duplicate(actor=self.dispatcher, report_id=self.source.pk, incident_id=incident.pk)
        self.source.review_status = "accepted"
        self.source.save()
        link_reports(actor=self.dispatcher, incident_id=incident.pk, report_ids=[self.source.pk])
        self.source.refresh_from_db()
        self.assertEqual(self.source.incident_id, incident.pk)

    def test_database_rejects_duplicate_or_invalid_dismissals(self):
        candidate = self.report()
        dismissal = dismiss_potential_duplicate(actor=self.dispatcher, report_id=self.source.pk, candidate_report_id=candidate.pk)
        for kwargs in (
            {"report": self.source, "candidate_report": candidate},
            {"report": self.source}, {"report": self.source, "candidate_report": self.source},
            {"report": candidate, "candidate_report": self.source},
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                DuplicateDismissal.objects.create(dismissed_by=self.dispatcher, **kwargs)
        self.assertTrue(DuplicateDismissal.objects.filter(pk=dismissal.pk).exists())
