from io import StringIO
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from accounts.models import Role, User
from dispatch.models import Assignment
from incidents.models import Incident, IncidentCategory
from teams.models import ResponseTeam


class OperationsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", role=Role.ADMIN)
        self.category = IncidentCategory.objects.create(name="Synthetic", code="synthetic")
        self.team = ResponseTeam.objects.create(name="Synthetic", code="synthetic", status="available", last_location=Point(106.7, 10.77, srid=4326))
        self.team.categories.add(self.category)
        self.client.force_authenticate(self.admin)

    def test_only_admin_can_manage_resources(self):
        for role in Role.values:
            user = User.objects.create_user(username=f"role-{role}", role=role)
            self.client.force_authenticate(user)
            for endpoint in ["users", "teams", "categories", "configuration"]:
                response = self.client.get(f"/api/v1/admin/{endpoint}/")
                self.assertEqual(response.status_code, 200 if role == Role.ADMIN else 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/admin/users/").status_code, 401)

    def test_create_user_password_membership_role_and_unknown_fields(self):
        body = {"username": "rescue", "password": "Synthetic-safe-pass-47!", "role": "rescue_team", "response_team": self.team.pk}
        response = self.client.post("/api/v1/admin/users/", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("password", response.data)
        user = User.objects.get(pk=response.data["id"])
        self.assertTrue(user.check_password(body["password"]))
        self.assertEqual(user.response_team, self.team)
        response = self.client.patch(f"/api/v1/admin/users/{user.pk}/", {"role": "dispatcher"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        user.refresh_from_db()
        self.assertIsNone(user.response_team)
        self.assertEqual(self.client.post("/api/v1/admin/users/", {**body, "username": "bad", "is_superuser": True}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/v1/admin/users/", {**body, "username": "weak", "password": "123"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/v1/admin/users/", {**body, "username": "bad-member", "role": "citizen"}, format="json").status_code, 400)

    def test_self_protection_deactivate_revoke_token_and_preserve_history(self):
        own = f"/api/v1/admin/users/{self.admin.pk}/"
        self.assertEqual(self.client.delete(own).status_code, 400)
        self.assertEqual(self.client.patch(own, {"role": "citizen"}, format="json").status_code, 400)
        citizen = User.objects.create_user(username="history")
        Token.objects.create(user=citizen)
        self.assertEqual(self.client.delete(f"/api/v1/admin/users/{citizen.pk}/").status_code, 204)
        citizen.refresh_from_db()
        self.assertFalse(citizen.is_active)
        self.assertFalse(Token.objects.filter(user=citizen).exists())

    def test_team_coordinates_category_and_assignment_guards(self):
        url = f"/api/v1/admin/teams/{self.team.pk}/"
        response = self.client.patch(url, {"latitude": 21, "longitude": 105, "categories": [self.category.pk]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.team.refresh_from_db()
        self.assertEqual((self.team.last_location.x, self.team.last_location.y, self.team.last_location.srid), (105, 21, 4326))
        self.assertEqual(self.client.patch(url, {"latitude": 91, "longitude": 105}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"latitude": 21}, format="json").status_code, 400)
        incident = Incident.objects.create(title="Synthetic", category=self.category, location=self.team.last_location)
        Assignment.objects.create(incident=incident, team=self.team, assigned_by=self.admin)
        self.team.status = "busy"
        self.team.save()
        for data in [{"status": "available"}, {"categories": []}, {"latitude": 21, "longitude": 105}]:
            self.assertEqual(self.client.patch(url, data, format="json").status_code, 409)
        self.assertEqual(self.client.delete(url).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/v1/admin/categories/{self.category.pk}/").status_code, 409)

    def test_category_crud_search_and_config_excludes_secrets(self):
        for data in [[{"name": "Invalid"}], "Invalid", None]:
            self.assertEqual(self.client.post("/api/v1/admin/categories/", data, format="json").status_code, 400)
        response = self.client.post("/api/v1/admin/categories/", {"name": "Flood", "code": "flood"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        pk = response.data["id"]
        self.assertEqual(self.client.get("/api/v1/admin/categories/?search=flood").data["count"], 1)
        self.assertEqual(self.client.patch(f"/api/v1/admin/categories/{pk}/", {"is_active": False}, format="json").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/admin/categories/{pk}/").status_code, 204)
        config = self.client.get("/api/v1/admin/configuration/").data
        self.assertTrue(config["read_only"])
        self.assertNotIn("DJANGO_SECRET_KEY", config["values"])


class FrontendShellTests(TestCase):
    def test_shell_assets_config_and_no_path_traversal(self):
        for path in ["", "app.js", "client.js", "map.js", "style.css", "config.json"]:
            self.assertEqual(self.client.get("/realtime/" + path).status_code, 200)
        self.assertEqual(self.client.get("/realtime/settings.py").status_code, 404)
        config = self.client.get("/realtime/config.json").json()
        self.assertEqual(config["apiBase"], "/api/v1/")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", str(config))

    @patch.dict("os.environ", {"DEMO_PASSWORD": "Synthetic-demo-pass-47!"})
    def test_demo_seed_is_idempotent_and_does_not_reset_password_or_readiness(self):
        call_command("seed_demo", stdout=StringIO())
        team = ResponseTeam.objects.get(code="demo-rescue")
        team.status = "offline"
        team.save()
        user = User.objects.get(username="demo_citizen")
        user.set_password("Synthetic-modified-pass-48!")
        user.save()
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(User.objects.count(), 6)
        self.assertEqual(ResponseTeam.objects.count(), 3)
        for code in ("demo-medical", "demo-backup"):
            self.assertTrue(ResponseTeam.objects.get(code=code).categories.exists())
        team.refresh_from_db()
        user.refresh_from_db()
        self.assertEqual(team.status, "offline")
        self.assertTrue(user.check_password("Synthetic-modified-pass-48!"))
        self.assertEqual(set(IncidentCategory.objects.values_list("code", flat=True)), {"fire", "traffic"})


class PhaseSevenWorkflowTests(APITestCase):
    def setUp(self):
        self.category = IncidentCategory.objects.create(name="Synthetic", code="workflow")
        self.team = ResponseTeam.objects.create(name="Synthetic", code="workflow", status="available", last_location=Point(106.7, 10.77, srid=4326))
        self.team.categories.add(self.category)
        self.citizen = User.objects.create_user(username="citizen", phone="+12025550123")
        self.dispatcher = User.objects.create_user(username="dispatcher", role=Role.DISPATCHER)
        self.rescue = User.objects.create_user(username="rescue", role=Role.RESCUE_TEAM, response_team=self.team)

    def test_real_rest_workflow_report_merge_dispatch_gps_complete_and_citizen_status(self):
        self.client.force_authenticate(self.citizen)
        reports = []
        for _ in range(2):
            response = self.client.post("/api/v1/incident-reports/", {"category": self.category.pk, "description": "Synthetic smoke", "latitude": 10.77, "longitude": 106.7}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            reports.append(response.data["id"])
        self.client.force_authenticate(self.dispatcher)
        self.assertEqual(self.client.get(f"/api/v1/incident-reports/{reports[0]}/potential-duplicates/").status_code, 200)
        for pk in reports:
            self.assertEqual(self.client.post(f"/api/v1/incident-reports/{pk}/verify/", {"review_status": "accepted"}, format="json").status_code, 200)
        response = self.client.post(f"/api/v1/incident-reports/{reports[0]}/create-incident/", {"title": "Synthetic fire"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        incident_id = response.data["id"]
        self.assertEqual(self.client.post(f"/api/v1/incidents/{incident_id}/reports/", {"report_ids": reports}, format="json").status_code, 200)
        suggested = self.client.get(f"/api/v1/incidents/{incident_id}/suggested-teams/")
        self.assertEqual(suggested.data["teams"][0]["id"], self.team.pk)
        response = self.client.post(f"/api/v1/incidents/{incident_id}/assignments/", {"team_id": self.team.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        assignment_id = response.data["id"]
        self.client.force_authenticate(self.rescue)
        self.assertEqual(self.client.get("/api/v1/assignments/").data["count"], 1)
        self.assertEqual(self.client.post(f"/api/v1/assignments/{assignment_id}/accept/", {"expected_status": "assigned"}, format="json").status_code, 200)
        from django.utils import timezone
        self.assertEqual(self.client.post("/api/v1/teams/me/location/", {"latitude": 10.771, "longitude": 106.701, "accuracy": 5, "timestamp": timezone.now().isoformat()}, format="json").status_code, 200)
        for previous, target in [("accepted", "en_route"), ("en_route", "on_scene"), ("on_scene", "responding"), ("responding", "completed")]:
            self.assertEqual(self.client.patch(f"/api/v1/assignments/{assignment_id}/status/", {"expected_status": previous, "status": target}, format="json").status_code, 200)
        self.client.force_authenticate(self.dispatcher)
        self.assertEqual(self.client.patch(f"/api/v1/incidents/{incident_id}/status/", {"expected_status": "in_progress", "status": "resolved"}, format="json").status_code, 200)
        self.client.force_authenticate(self.citizen)
        self.assertEqual(self.client.get(f"/api/v1/incident-reports/{reports[0]}/").data["incident_status"], "resolved")
        self.assertEqual(self.client.get(f"/api/v1/incidents/{incident_id}/").status_code, 403)
