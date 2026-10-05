from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.cache import cache
from rest_framework.test import APITestCase

from accounts.models import Role, User
from dispatch.services import assign_team, cancel_assignment
from incidents import services
from incidents.models import IncidentCategory, IncidentReport
from teams.models import ResponseTeam


class ReportLocationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.citizen = User.objects.create_user(username="synthetic-location", first_name="Synthetic", last_name="Citizen", phone="+12025550123")
        self.dispatcher = User.objects.create_user(username="synthetic-dispatcher", role=Role.DISPATCHER)
        self.category = IncidentCategory.objects.create(code="synthetic-location", name="Synthetic")
        self.client.force_authenticate(self.citizen)

    def payload(self, **changes):
        return {"category": self.category.pk, "description": "Synthetic field report", "latitude": 21, "longitude": 105, **changes}

    def test_profile_autofill_default_contact_and_own_profile(self):
        self.assertEqual(self.client.get("/api/v1/auth/me/").data["phone"], self.citizen.phone)
        response = self.client.post("/api/v1/incident-reports/", self.payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        report = IncidentReport.objects.get(pk=response.data["id"])
        self.assertEqual(report.reporter_name, "Synthetic Citizen")
        self.assertEqual(report.reporter_phone, self.citizen.phone)
        self.assertTrue(report.allow_contact)
        self.assertIsNone(report.gps_location)
        self.client.force_authenticate(self.dispatcher)
        contact = self.client.get(f"/api/v1/incident-reports/{report.pk}/contact/")
        self.assertEqual(contact.data["reporter_phone"], self.citizen.phone)
        self.assertTrue(contact.data["can_call"])
        self.assertEqual(contact["Cache-Control"], "no-store")

    def test_gps_is_nullable_and_never_replaces_spatial_incident_location(self):
        response = self.client.post("/api/v1/incident-reports/", self.payload(gps_location={"latitude": 10, "longitude": 106}), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        report = IncidentReport.objects.get(pk=response.data["id"])
        self.assertEqual(report.location, Point(105, 21, srid=4326))
        self.assertEqual(report.gps_location, Point(106, 10, srid=4326))
        services.review_report(actor=self.dispatcher, report_id=report.pk, review_status="accepted")
        incident = services.create_incident_from_report(actor=self.dispatcher, report_id=report.pk, title="Synthetic site")
        self.assertEqual(incident.location, report.location)
        self.assertNotEqual(incident.location, report.gps_location)

    def test_invalid_contact_and_gps_are_rejected(self):
        for changes in ({"reporter_phone": ""}, {"reporter_phone": "tel:123"}, {"reporter_name": " "},
                        {"gps_location": {"latitude": 91, "longitude": 105}},
                        {"gps_location": {"latitude": True, "longitude": 105}},
                        {"gps_location": {"latitude": 21}},
                        {"gps_location": {"latitude": "NaN", "longitude": 105}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post("/api/v1/incident-reports/", self.payload(**changes), format="json").status_code, 400)
        self.assertFalse(IncidentReport.objects.exists())

    def test_assigned_team_gets_contact_without_extra_confirmation_then_loses_access(self):
        response = self.client.post("/api/v1/incident-reports/", self.payload(), format="json")
        report = IncidentReport.objects.get(pk=response.data["id"])
        services.review_report(actor=self.dispatcher, report_id=report.pk, review_status="accepted")
        incident = services.create_incident_from_report(actor=self.dispatcher, report_id=report.pk, title="Synthetic incident")
        team = ResponseTeam.objects.create(code="synthetic-team", name="Synthetic team", status="available")
        team.categories.add(self.category)
        rescue = User.objects.create_user(username="synthetic-rescue", role=Role.RESCUE_TEAM, response_team=team)
        outsider = User.objects.create_user(username="synthetic-outsider", role=Role.RESCUE_TEAM)
        assignment = assign_team(actor=self.dispatcher, incident_id=incident.pk, team_id=team.pk)
        endpoint = f"/api/v1/assignments/{assignment.pk}/contacts/"
        self.client.force_authenticate(rescue)
        self.assertEqual(self.client.get(endpoint).data[0]["reporter_phone"], self.citizen.phone)
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(endpoint).status_code, 404)
        cancel_assignment(actor=self.dispatcher, assignment_id=assignment.pk, expected_status=assignment.status)
        self.client.force_authenticate(rescue)
        self.assertEqual(self.client.get(endpoint).status_code, 409)
        self.assertEqual(self.client.get(f"/api/v1/incident-reports/{report.pk}/contact/").status_code, 404)

    def test_no_contact_data_in_new_report_event(self):
        with patch("realtime.events.deliver") as delivery, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/v1/incident-reports/", self.payload(), format="json")
        self.assertEqual(response.status_code, 201)
        payload = delivery.call_args.args[1]["data"]
        self.assertEqual(set(payload), {"report_id", "review_status", "incident_id", "updated_at"})
