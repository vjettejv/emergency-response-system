from django.contrib.gis.geos import Point
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import Role, User
from dispatch.models import Assignment
from incidents.models import Incident, IncidentCategory, IncidentReport, IncidentStatus, IncidentStatusHistory
from teams.models import ResponseTeam


class CoreModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="synthetic-dispatcher", role=Role.DISPATCHER)
        self.category = IncidentCategory.objects.create(name="Synthetic category", code="test")
        self.incident = Incident.objects.create(
            title="Synthetic incident", category=self.category,
            location=Point(105.8, 21.0, srid=4326), created_by=self.user,
        )
        self.team = ResponseTeam.objects.create(name="Synthetic team", code="team-1")

    def test_spatial_data_round_trip_and_report_link(self):
        citizen = User.objects.create_user(username="citizen")
        report = IncidentReport.objects.create(
            reporter=citizen, category=self.category, description="Synthetic report",
            location=Point(105.81, 21.01, srid=4326),
        )
        self.assertIsNone(report.incident_id)
        report.incident = self.incident
        report.save(update_fields=["incident"])
        report.refresh_from_db()
        self.assertAlmostEqual(report.location.x, 105.81)
        self.assertAlmostEqual(report.location.y, 21.01)
        self.assertEqual(report.location.srid, 4326)
        self.assertEqual(self.incident.reports.count(), 1)

    def test_team_cannot_have_two_active_assignments(self):
        Assignment.objects.create(incident=self.incident, team=self.team, assigned_by=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment.objects.create(incident=self.incident, team=self.team, assigned_by=self.user)

    def test_completed_assignment_allows_reassignment(self):
        assignment = Assignment.objects.create(incident=self.incident, team=self.team, assigned_by=self.user)
        assignment.status = Assignment.Status.COMPLETED
        assignment.ended_at = timezone.now()
        assignment.save(update_fields=["status", "ended_at"])
        Assignment.objects.create(incident=self.incident, team=self.team, assigned_by=self.user)
        self.assertEqual(self.team.assignments.count(), 2)

    def test_terminal_assignment_requires_end_time(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment.objects.create(
                incident=self.incident, team=self.team, assigned_by=self.user,
                status=Assignment.Status.COMPLETED,
            )

    def test_invalid_incident_status_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Incident.objects.filter(pk=self.incident.pk).update(status="invalid")

    def test_only_rescue_role_can_belong_to_team(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=self.user.pk).update(response_team=self.team)

    def test_superuser_must_be_admin(self):
        admin = User.objects.create_superuser(username="admin", password="Synthetic-admin-pass-42!")
        self.assertEqual(admin.role, Role.ADMIN)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=admin.pk).update(role=Role.CITIZEN)

    def test_history_records_actor_and_rejects_unchanged_status(self):
        history = IncidentStatusHistory.objects.create(
            incident=self.incident, from_status="", to_status=IncidentStatus.NEW, changed_by=self.user,
        )
        self.assertEqual(history.changed_by, self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            IncidentStatusHistory.objects.create(
                incident=self.incident, from_status=IncidentStatus.NEW,
                to_status=IncidentStatus.NEW, changed_by=self.user,
            )
