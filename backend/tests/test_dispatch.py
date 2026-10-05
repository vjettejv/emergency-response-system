from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.db import connection, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.test import APITestCase

from accounts.models import Role, User
from dispatch import services
from dispatch.models import Assignment, AssignmentHistory
from dispatch.suggestions import eligible_team_query, suggest_teams
from incidents.models import Incident, IncidentCategory
from incidents.services import Conflict, change_incident_status
from teams.models import ResponseTeam


@override_settings(DISPATCH_RADIUS_METERS=1000, DISPATCH_MAX_SUGGESTIONS=20)
class DispatchTests(APITestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username="dispatcher", role=Role.DISPATCHER)
        self.admin = User.objects.create_user(username="admin", role=Role.ADMIN)
        self.citizen = User.objects.create_user(username="citizen")
        self.category = IncidentCategory.objects.create(code="synthetic", name="Synthetic")
        self.incident = self.make_incident()
        self.team = self.make_team("first")
        self.rescue = User.objects.create_user(username="rescue", role=Role.RESCUE_TEAM, response_team=self.team)
        self.client.force_authenticate(self.manager)

    def make_incident(self, **kwargs):
        data = {"title": "Synthetic incident", "category": self.category, "location": Point(105, 21, srid=4326), "status": "verified"}
        data.update(kwargs)
        return Incident.objects.create(**data)

    def make_team(self, code, supports=True, **kwargs):
        data = {
            "name": f"Synthetic {code}", "code": code, "status": "available",
            "last_location": Point(105, 21, srid=4326), "location_updated_at": timezone.now(),
        }
        data.update(kwargs)
        team = ResponseTeam.objects.create(**data)
        if supports:
            team.categories.add(self.category)
        return team

    def assign(self, team=None, incident=None):
        return services.assign_team(actor=self.manager, incident_id=(incident or self.incident).pk, team_id=(team or self.team).pk)

    def transition(self, assignment, target, expected):
        return services.transition_assignment(
            actor=self.rescue, assignment_id=assignment.pk, status=target, expected_status=expected,
        )

    def test_suggestions_filter_support_readiness_location_distance_and_active_work(self):
        near = self.make_team("near", last_location=Point(105.001, 21, srid=4326))
        self.make_team("far", last_location=Point(105.1, 21, srid=4326))
        self.make_team("unsupported", supports=False)
        self.make_team("offline", status="offline")
        self.make_team("busy", status="busy")
        self.make_team("unknown", last_location=None)
        occupied = self.make_team("occupied")
        # A legacy/inconsistent available flag must not override the active assignment.
        Assignment.objects.create(team=occupied, incident=self.incident, assigned_by=self.manager)
        with self.assertNumQueries(2):
            result = suggest_teams(actor=self.manager, incident_id=self.incident.pk)
        self.assertEqual([t.pk for t in result["teams"]], [self.team.pk, near.pk])
        self.assertAlmostEqual(result["teams"][0].distance_meters, 0)
        self.assertGreater(result["teams"][1].distance_meters, 100)
        self.assertFalse(result["routing_available"])
        self.assertIsNone(result["teams"][0].eta_seconds)

    def test_radius_and_result_limit_are_configurable(self):
        self.make_team("near", last_location=Point(105.001, 21, srid=4326))
        with override_settings(DISPATCH_RADIUS_METERS=50):
            result = suggest_teams(actor=self.manager, incident_id=self.incident.pk)
            self.assertEqual(len(result["teams"]), 1)
        with override_settings(DISPATCH_MAX_SUGGESTIONS=1):
            result = suggest_teams(actor=self.manager, incident_id=self.incident.pk)
            self.assertEqual(len(result["teams"]), 1)
            self.assertTrue(result["more_teams"])

    def test_spatial_query_and_gist_eligibility(self):
        query = eligible_team_query(self.incident)
        sql, _ = query.query.sql_with_params()
        self.assertIn("ST_DWithin", sql)
        self.assertIn("ST_Distance", sql)
        self.assertIn("EXISTS", sql)
        # Validate the spatial predicate can use the existing geography index.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("EXPLAIN SELECT id FROM teams_responseteam WHERE ST_DWithin(last_location, ST_SetSRID(ST_MakePoint(105, 21), 4326)::geography, 1000)")
            plan = "\n".join(row[0] for row in cursor.fetchall())
        self.assertIn("Index", plan)
        self.assertIn("last_location", plan)

    def test_suggestion_api_contains_distance_and_null_eta(self):
        response = self.client.get(reverse("dispatch:suggested-teams", args=[self.incident.pk]))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["teams"][0]["status"], "available")
        self.assertEqual(response.data["teams"][0]["longitude"], 105)
        self.assertEqual(response.data["teams"][0]["distance_meters"], 0)
        self.assertIsNone(response.data["teams"][0]["eta_seconds"])

    def test_assign_sets_team_busy_incident_dispatched_and_audits(self):
        response = self.client.post(reverse("dispatch:assign-team", args=[self.incident.pk]), {"team_id": self.team.pk})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "assigned")
        assignment = Assignment.objects.get(pk=response.data["id"])
        self.assertEqual(assignment.status, "pending")
        self.team.refresh_from_db()
        self.incident.refresh_from_db()
        self.assertEqual(self.team.status, "busy")
        self.assertEqual(self.incident.status, "dispatched")
        self.assertEqual(assignment.history.get().operation, "assign")
        self.assertEqual(self.incident.status_history.get().to_status, "dispatched")

    def test_unavailable_unsupported_and_occupied_teams_are_rejected(self):
        offline = self.make_team("offline", status="offline")
        unsupported = self.make_team("unsupported", supports=False)
        occupied = self.make_team("occupied")
        Assignment.objects.create(team=occupied, incident=self.incident, assigned_by=self.manager)
        for team in (offline, unsupported, occupied):
            with self.assertRaises(Conflict):
                self.assign(team=team)
        self.assertEqual(Assignment.objects.count(), 1)

    def test_closed_or_unverified_incident_rejects_assignments_and_suggestions(self):
        for status in ("new", "resolved", "cancelled"):
            incident = self.make_incident(status=status)
            with self.assertRaises(Conflict):
                self.assign(incident=incident)
            with self.assertRaises(Conflict):
                suggest_teams(actor=self.manager, incident_id=incident.pk)

    def test_full_rescue_workflow_and_explicit_incident_resolution(self):
        assignment = self.assign()
        self.client.force_authenticate(self.rescue)
        response = self.client.post(reverse("dispatch:assignment-accept", args=[assignment.pk]), {"expected_status": "assigned"})
        self.assertEqual(response.status_code, 200, response.data)
        for before, after in (("accepted", "en_route"), ("en_route", "on_scene"), ("on_scene", "responding"), ("responding", "completed")):
            response = self.client.patch(reverse("dispatch:assignment-change-status", args=[assignment.pk]), {
                "expected_status": before, "status": after,
            })
            self.assertEqual(response.status_code, 200, response.data)
        assignment.refresh_from_db()
        self.team.refresh_from_db()
        self.incident.refresh_from_db()
        self.assertEqual(assignment.status, "completed")
        self.assertIsNotNone(assignment.ended_at)
        self.assertEqual(self.team.status, "available")
        self.assertEqual(self.incident.status, "in_progress")
        self.assertEqual(assignment.history.count(), 6)
        change_incident_status(actor=self.manager, incident_id=self.incident.pk, status="resolved", expected_status="in_progress")
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, "resolved")

    def test_invalid_skip_backward_and_stale_transitions(self):
        assignment = self.assign()
        for target, expected in (("completed", "pending"), ("on_scene", "pending"), ("accepted", "accepted")):
            with self.assertRaises(Conflict):
                self.transition(assignment, target, expected)
        self.transition(assignment, "accepted", "pending")
        with self.assertRaises(Conflict):
            self.transition(assignment, "pending", "accepted")
        self.assertEqual(assignment.history.count(), 2)

    def test_same_state_request_is_noop(self):
        assignment = self.assign()
        self.transition(assignment, "accepted", "pending")
        self.transition(assignment, "accepted", "accepted")
        self.assertEqual(assignment.history.count(), 2)

    def test_cancel_releases_team_and_restores_waiting_incident(self):
        assignment = self.assign()
        cancelled = services.cancel_assignment(actor=self.manager, assignment_id=assignment.pk, expected_status="pending", note="Cancel demo")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNotNone(cancelled.ended_at)
        self.team.refresh_from_db()
        self.incident.refresh_from_db()
        self.assertEqual(self.team.status, "available")
        self.assertEqual(self.incident.status, "verified")
        self.assertEqual(assignment.history.last().note, "Cancel demo")

    def test_multiple_teams_cancellation_does_not_reset_incident_early(self):
        first = self.assign()
        second = self.assign(team=self.make_team("second"))
        services.cancel_assignment(actor=self.manager, assignment_id=first.pk, expected_status="pending")
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, "dispatched")
        services.cancel_assignment(actor=self.manager, assignment_id=second.pk, expected_status="pending")
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, "verified")

    def test_rejected_assignment_releases_team(self):
        assignment = self.assign()
        self.transition(assignment, "rejected", "pending")
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, "available")
        with self.assertRaises(Conflict):
            self.transition(assignment, "accepted", "rejected")

    def test_reassign_preserves_old_assignment_and_links_replacement(self):
        original = self.assign()
        second = self.make_team("second")
        response = self.client.post(reverse("dispatch:assignment-reassign", args=[original.pk]), {
            "team_id": second.pk, "expected_status": "assigned", "note": "Replace demo team",
        })
        self.assertEqual(response.status_code, 201, response.data)
        original.refresh_from_db()
        replacement = Assignment.objects.get(pk=response.data["id"])
        self.assertEqual(original.status, "cancelled")
        self.assertEqual(original.history.last().operation, "reassign")
        self.assertEqual(replacement.supersedes_id, original.pk)
        self.assertEqual(replacement.status, "pending")
        self.team.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.team.status, "available")
        self.assertEqual(second.status, "busy")
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, "dispatched")

    def test_invalid_replacement_does_not_cancel_original(self):
        original = self.assign()
        unsupported = self.make_team("unsupported", supports=False)
        for team in (unsupported, self.team):
            with self.assertRaises(Conflict):
                services.reassign_team(actor=self.manager, assignment_id=original.pk, team_id=team.pk, expected_status="pending")
        original.refresh_from_db()
        self.assertEqual(original.status, "pending")
        self.assertEqual(Assignment.objects.count(), 1)

    def test_terminal_assignments_cannot_be_cancelled_or_replaced(self):
        assignment = self.assign()
        services.cancel_assignment(actor=self.manager, assignment_id=assignment.pk, expected_status="pending")
        second = self.make_team("second")
        with self.assertRaises(Conflict):
            services.cancel_assignment(actor=self.manager, assignment_id=assignment.pk, expected_status="cancelled")
        with self.assertRaises(Conflict):
            services.reassign_team(actor=self.manager, assignment_id=assignment.pk, team_id=second.pk, expected_status="cancelled")

    def test_incident_cannot_close_with_active_work(self):
        assignment = self.assign()
        for target in ("resolved", "cancelled"):
            # Manual in_progress is allowed, but closing with active teams is not.
            self.incident.status = "in_progress"
            self.incident.save()
            with self.assertRaises(Conflict):
                change_incident_status(actor=self.manager, incident_id=self.incident.pk, status=target, expected_status="in_progress")
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, "pending")

    def test_rescue_can_only_access_own_team_assignments_and_history(self):
        own = self.assign()
        other = self.assign(team=self.make_team("other"))
        self.client.force_authenticate(self.rescue)
        response = self.client.get(reverse("dispatch:assignment-list"))
        self.assertEqual([row["id"] for row in response.data["results"]], [own.pk])
        for name in ("assignment-detail", "assignment-history"):
            self.assertEqual(self.client.get(reverse(f"dispatch:{name}", args=[other.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("dispatch:assignment-accept", args=[other.pk]), {"expected_status": "assigned"}).status_code, 404)
        with self.assertRaises(NotFound):
            self.transition(other, "accepted", "pending")

    def test_rescue_without_team_sees_no_assignments(self):
        self.assign()
        self.rescue.response_team = None
        self.rescue.save()
        self.client.force_authenticate(self.rescue)
        self.assertEqual(self.client.get(reverse("dispatch:assignment-list")).data["count"], 0)

    def test_dispatcher_admin_and_rescue_role_boundaries(self):
        assignment = self.assign()
        for actor in (self.citizen, self.rescue):
            self.client.force_authenticate(actor)
            self.assertEqual(self.client.get(reverse("dispatch:suggested-teams", args=[self.incident.pk])).status_code, 403)
            self.assertEqual(self.client.post(reverse("dispatch:assign-team", args=[self.incident.pk]), {"team_id": self.team.pk}).status_code, 403)
            self.assertEqual(self.client.post(reverse("dispatch:assignment-cancel", args=[assignment.pk]), {"expected_status": "assigned"}).status_code, 403)
        for actor in (self.manager, self.admin):
            self.client.force_authenticate(actor)
            self.assertEqual(self.client.get(reverse("dispatch:assignment-list")).status_code, 200)
            self.assertEqual(self.client.post(reverse("dispatch:assignment-accept", args=[assignment.pk]), {"expected_status": "assigned"}).status_code, 403)
        self.client.force_authenticate(self.citizen)
        self.assertEqual(self.client.get(reverse("dispatch:assignment-list")).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse("dispatch:assignment-list")).status_code, 401)

    def test_admin_can_assign_and_cancel(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(reverse("dispatch:assign-team", args=[self.incident.pk]), {"team_id": self.team.pk})
        self.assertEqual(response.status_code, 201)
        cancelled = self.client.post(reverse("dispatch:assignment-cancel", args=[response.data["id"]]), {"expected_status": "assigned"})
        self.assertEqual(cancelled.status_code, 200)

    def test_service_role_checks_cannot_be_bypassed(self):
        assignment = self.assign()
        with self.assertRaises(PermissionDenied):
            services.cancel_assignment(actor=self.rescue, assignment_id=assignment.pk, expected_status="pending")
        with self.assertRaises(PermissionDenied):
            services.assign_team(actor=self.citizen, incident_id=self.incident.pk, team_id=self.team.pk)
        with self.assertRaises(PermissionDenied):
            services.transition_assignment(actor=self.manager, assignment_id=assignment.pk, status="accepted", expected_status="pending")

    def test_filters_pagination_and_history_status_alias(self):
        assignment = self.assign()
        self.assign(team=self.make_team("second"))
        response = self.client.get(reverse("dispatch:assignment-list"), {"status": "assigned", "team": self.team.pk, "incident": self.incident.pk})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], assignment.pk)
        page = self.client.get(reverse("dispatch:assignment-list"), {"page_size": 1})
        self.assertIsNotNone(page.data["next"])
        history = self.client.get(reverse("dispatch:assignment-history", args=[assignment.pk]))
        self.assertEqual(history.data["results"][0]["to_status"], "assigned")
        self.assertEqual(self.client.get(reverse("dispatch:assignment-list"), {"status": "invalid"}).status_code, 400)

    def test_invalid_inputs_missing_objects_and_unexposed_updates(self):
        url = reverse("dispatch:assign-team", args=[self.incident.pk])
        for body in ({}, {"team_id": 0}, {"team_id": self.team.pk, "status": "completed"}):
            self.assertEqual(self.client.post(url, body).status_code, 400)
        self.assertEqual(self.client.post(url, {"team_id": 999999}).status_code, 404)
        assignment = self.assign()
        detail = reverse("dispatch:assignment-detail", args=[assignment.pk])
        self.assertEqual(self.client.patch(detail, {"team": 123}).status_code, 405)
        self.assertEqual(self.client.delete(detail).status_code, 405)
        self.assertEqual(self.client.post(reverse("dispatch:assignment-cancel", args=[assignment.pk]), {}).status_code, 400)

    def test_assignment_audit_failure_rolls_back_all_changes(self):
        with patch("dispatch.services.AssignmentHistory.objects.create", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.assign()
        self.team.refresh_from_db()
        self.incident.refresh_from_db()
        self.assertEqual(self.team.status, "available")
        self.assertEqual(self.incident.status, "verified")
        self.assertFalse(Assignment.objects.exists())

    def test_incident_history_failure_rolls_back_assignment_audit_too(self):
        with patch("dispatch.services.IncidentStatusHistory.objects.create", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.assign()
        self.assertFalse(Assignment.objects.exists())
        self.assertFalse(AssignmentHistory.objects.exists())
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, "available")

    def test_failed_reassignment_audit_restores_original_team_and_assignment(self):
        original = self.assign()
        second = self.make_team("second")
        record = services._record

        def fail_new_assignment(assignment, actor, operation, previous, note):
            if operation == AssignmentHistory.Operation.ASSIGN:
                raise RuntimeError("audit failed")
            return record(assignment, actor, operation, previous, note)

        with patch("dispatch.services._record", side_effect=fail_new_assignment):
            with self.assertRaises(RuntimeError):
                services.reassign_team(actor=self.manager, assignment_id=original.pk, team_id=second.pk, expected_status="pending")
        original.refresh_from_db()
        self.team.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(original.status, "pending")
        self.assertIsNone(original.ended_at)
        self.assertEqual(self.team.status, "busy")
        self.assertEqual(second.status, "available")
        self.assertEqual(original.history.count(), 1)
        self.assertEqual(Assignment.objects.count(), 1)
