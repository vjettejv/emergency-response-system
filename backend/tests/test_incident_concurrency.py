from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from django.contrib.gis.geos import Point
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from accounts.models import Role, User
from incidents.clustering import dismiss_potential_duplicate
from incidents.models import DuplicateDismissal, Incident, IncidentCategory, IncidentReport, IncidentReportLinkHistory
from incidents.services import Conflict, change_incident_status, create_incident_from_report, link_reports, save_report_draft, finalize_report


class IncidentConcurrencyTests(TransactionTestCase):
    """Use separate real PostgreSQL connections to exercise row locking."""

    def setUp(self):
        self.actor = User.objects.create_user(username="dispatcher", role=Role.DISPATCHER)
        self.citizen = User.objects.create_user(username="citizen")
        self.category = IncidentCategory.objects.create(code="synthetic", name="Synthetic")
        self.report = IncidentReport.objects.create(
            reporter=self.citizen, category=self.category, description="Synthetic",
            location=Point(105, 21, srid=4326), review_status="accepted",
        )

    def race(self, operations, expected=("ok", "conflict")):
        barrier = Barrier(len(operations))

        def execute(operation):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                actor = User.objects.get(pk=self.actor.pk)
                barrier.wait(timeout=10)
                try:
                    operation(actor)
                    return "ok"
                except Conflict:
                    return "conflict"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            results = list(pool.map(execute, operations, timeout=20))
        self.assertCountEqual(results, expected)

    def test_concurrent_creation_produces_one_incident(self):
        def create(actor):
            create_incident_from_report(actor=actor, report_id=self.report.pk, title="Synthetic")

        self.race([create, create])
        self.assertEqual(Incident.objects.count(), 1)
        self.report.refresh_from_db()
        self.assertEqual(self.report.incident.status_history.count(), 1)
        self.assertEqual(self.report.incident.report_link_history.count(), 1)

    def test_concurrent_draft_retry_creates_one_report_and_finalization_timestamp(self):
        request_id = uuid4()

        def save(_):
            save_report_draft(actor=User.objects.get(pk=self.citizen.pk), request_id=request_id, values={
                "category": self.category, "description": "Synthetic draft",
                "location": Point(105, 21, srid=4326),
                "reporter_name": "Synthetic Citizen", "reporter_phone": "+12025550123",
            })

        self.race([save, save], expected=("ok", "ok"))
        self.assertEqual(IncidentReport.objects.filter(draft_request_id=request_id).count(), 1)
        draft = IncidentReport.objects.get(draft_request_id=request_id)

        def submit(_):
            finalize_report(actor=User.objects.get(pk=self.citizen.pk), report_id=draft.pk)

        self.race([submit, submit], expected=("ok", "ok"))
        draft.refresh_from_db()
        self.assertFalse(draft.is_draft)
        self.assertIsNotNone(draft.submitted_at)

    def test_concurrent_link_cannot_move_report_between_incidents(self):
        incidents = [Incident.objects.create(
            title="Synthetic", category=self.category, location=Point(105, 21, srid=4326), status="verified",
        ) for _ in range(2)]
        self.race([
            lambda actor: link_reports(actor=actor, incident_id=incidents[0].pk, report_ids=[self.report.pk]),
            lambda actor: link_reports(actor=actor, incident_id=incidents[1].pk, report_ids=[self.report.pk]),
        ])
        self.report.refresh_from_db()
        self.assertIn(self.report.incident_id, [item.pk for item in incidents])
        self.assertEqual(sum(item.reports.count() for item in incidents), 1)
        self.assertEqual(IncidentReportLinkHistory.objects.count(), 1)

    def test_concurrent_reversed_dismissals_create_one_decision(self):
        candidate = IncidentReport.objects.create(
            reporter=self.citizen, category=self.category, description="Synthetic",
            location=Point(105, 21, srid=4326),
        )
        self.race([
            lambda actor: dismiss_potential_duplicate(
                actor=actor, report_id=self.report.pk, candidate_report_id=candidate.pk,
            ),
            lambda actor: dismiss_potential_duplicate(
                actor=actor, report_id=candidate.pk, candidate_report_id=self.report.pk,
            ),
        ], expected=("ok", "ok"))
        self.assertEqual(DuplicateDismissal.objects.count(), 1)

    def test_concurrent_status_changes_reject_stale_request(self):
        incident = Incident.objects.create(
            title="Synthetic", category=self.category, location=Point(105, 21, srid=4326), status="verified",
        )
        self.race([
            lambda actor: change_incident_status(
                actor=actor, incident_id=incident.pk, status="in_progress", expected_status="verified",
            ),
            lambda actor: change_incident_status(
                actor=actor, incident_id=incident.pk, status="cancelled", expected_status="verified",
            ),
        ])
        self.assertEqual(incident.status_history.count(), 1)
