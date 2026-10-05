from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.gis.geos import Point
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from accounts.models import Role, User
from dispatch.models import ACTIVE_ASSIGNMENT_STATUSES, Assignment, AssignmentHistory
from dispatch.services import assign_team, cancel_assignment, reassign_team, transition_assignment
from incidents.models import Incident, IncidentCategory
from incidents.services import Conflict, change_incident_status
from teams.models import ResponseTeam


class DispatchConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="dispatcher", role=Role.DISPATCHER)
        self.category = IncidentCategory.objects.create(code="synthetic", name="Synthetic")
        self.incidents = [Incident.objects.create(
            title="Synthetic", category=self.category, location=Point(105, 21, srid=4326), status="verified",
        ) for _ in range(2)]
        self.teams = [ResponseTeam.objects.create(name=f"Synthetic {i}", code=f"team-{i}", status="available") for i in range(2)]
        for team in self.teams:
            team.categories.add(self.category)
        self.rescue = User.objects.create_user(username="rescue", role=Role.RESCUE_TEAM, response_team=self.teams[0])

    def race(self, operations):
        barrier = Barrier(len(operations))

        def execute(operation):
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
            results = list(pool.map(execute, operations, timeout=20))
        self.assertCountEqual(results, ["ok", "conflict"])

    def test_same_team_cannot_be_dispatched_to_two_incidents(self):
        self.race([
            lambda: assign_team(actor=self.manager, incident_id=self.incidents[0].pk, team_id=self.teams[0].pk),
            lambda: assign_team(actor=self.manager, incident_id=self.incidents[1].pk, team_id=self.teams[0].pk),
        ])
        self.assertEqual(Assignment.objects.count(), 1)
        self.assertEqual(AssignmentHistory.objects.count(), 1)

    def test_cancellation_and_acceptance_cannot_both_apply_to_stale_state(self):
        assignment = assign_team(actor=self.manager, incident_id=self.incidents[0].pk, team_id=self.teams[0].pk)
        self.race([
            lambda: cancel_assignment(actor=self.manager, assignment_id=assignment.pk, expected_status="pending"),
            lambda: transition_assignment(actor=self.rescue, assignment_id=assignment.pk, expected_status="pending", status="accepted"),
        ])
        assignment.refresh_from_db()
        self.teams[0].refresh_from_db()
        self.assertEqual(self.teams[0].status, "available" if assignment.status == "cancelled" else "busy")
        self.assertEqual(assignment.history.count(), 2)

    def test_assign_and_close_incident_cannot_leave_active_work_on_closed_incident(self):
        self.race([
            lambda: assign_team(actor=self.manager, incident_id=self.incidents[0].pk, team_id=self.teams[0].pk),
            lambda: change_incident_status(actor=self.manager, incident_id=self.incidents[0].pk, status="cancelled", expected_status="verified"),
        ])
        incident = Incident.objects.get(pk=self.incidents[0].pk)
        if incident.status == "cancelled":
            self.assertFalse(incident.assignments.exists())
        else:
            self.assertEqual(incident.status, "dispatched")
            self.assertEqual(incident.assignments.count(), 1)

    def test_competing_reassignment_and_new_dispatch_share_one_team_lock(self):
        original = assign_team(actor=self.manager, incident_id=self.incidents[0].pk, team_id=self.teams[0].pk)
        self.race([
            lambda: reassign_team(actor=self.manager, assignment_id=original.pk, team_id=self.teams[1].pk, expected_status="pending"),
            lambda: assign_team(actor=self.manager, incident_id=self.incidents[1].pk, team_id=self.teams[1].pk),
        ])
        self.assertEqual(Assignment.objects.filter(team=self.teams[1], status__in=ACTIVE_ASSIGNMENT_STATUSES).count(), 1)
