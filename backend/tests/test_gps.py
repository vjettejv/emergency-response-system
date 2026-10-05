from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role, User
from teams.models import ResponseTeam
from teams.services import update_location


class GPSTests(APITestCase):
    def setUp(self):
        self.team = ResponseTeam.objects.create(name="Synthetic", code="gps")
        self.other = ResponseTeam.objects.create(name="Other", code="other")
        self.rescue = User.objects.create_user(username="rescue", role=Role.RESCUE_TEAM, response_team=self.team)
        self.manager = User.objects.create_user(username="manager", role=Role.DISPATCHER)
        self.client.force_authenticate(self.rescue)
        self.url = "/api/v1/teams/me/location/"

    def payload(self, **kwargs):
        return {"latitude": 21.01, "longitude": 105.02, "accuracy": 8.5,
                "timestamp": timezone.now().isoformat(), **kwargs}

    def test_gps_persists_coordinates_accuracy_and_broadcasts_only_after_commit(self):
        with patch("realtime.events.get_channel_layer") as layer:
            layer.return_value.group_send = AsyncMock()
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url, self.payload(), format="json")
                self.assertEqual(response.status_code, 200, response.data)
                layer.return_value.group_send.assert_not_called()
            args = layer.return_value.group_send.call_args.args
            self.assertEqual(args[0], "dispatchers")
            self.assertEqual(args[1]["payload"]["type"], "team.location_updated")
        self.team.refresh_from_db()
        self.assertEqual(self.team.last_location.srid, 4326)
        self.assertEqual((self.team.last_location.x, self.team.last_location.y), (105.02, 21.01))
        self.assertEqual(self.team.location_accuracy, 8.5)
        self.assertIsNotNone(self.team.location_received_at)
        self.other.refresh_from_db()
        self.assertIsNone(self.other.last_location)

    def test_invalid_numbers_fields_and_timestamps(self):
        for change in [
            {"latitude": 91}, {"longitude": -181}, {"latitude": True},
            {"latitude": "NaN"}, {"longitude": "Infinity"}, {"accuracy": "NaN"},
            {"accuracy": -1}, {"accuracy": 10001}, {"team_id": self.other.pk},
            {"timestamp": "2026-09-28T00:00:00"}, {"timestamp": "invalid"},
            {"timestamp": (timezone.now()-timedelta(minutes=3)).isoformat()},
            {"timestamp": (timezone.now()+timedelta(minutes=2)).isoformat()},
        ]:
            self.assertEqual(self.client.post(self.url, self.payload(**change), format="json").status_code, 400, change)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 400)
        self.team.refresh_from_db()
        self.assertIsNone(self.team.last_location)

    def test_rate_limit_and_old_or_equal_samples_do_not_overwrite(self):
        data = self.payload()
        self.assertEqual(self.client.post(self.url, data, format="json").status_code, 200)
        self.assertEqual(self.client.post(self.url, data, format="json").status_code, 409)
        self.assertEqual(self.client.post(self.url, self.payload(latitude=22), format="json").status_code, 429)
        self.team.refresh_from_db()
        self.assertEqual(self.team.last_location.y, 21.01)
        ResponseTeam.objects.filter(pk=self.team.pk).update(location_received_at=timezone.now()-timedelta(seconds=10))
        self.assertEqual(self.client.post(self.url, self.payload(latitude=22), format="json").status_code, 200)

    def test_roles_membership_and_location_snapshot_permissions(self):
        for role in (Role.CITIZEN, Role.DISPATCHER, Role.ADMIN):
            user = User.objects.create_user(username=role, role=role)
            self.client.force_authenticate(user)
            self.assertEqual(self.client.post(self.url, self.payload(), format="json").status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(self.url, self.payload(), format="json").status_code, 401)
        self.rescue.response_team = None
        self.rescue.save()
        self.client.force_authenticate(self.rescue)
        self.assertEqual(self.client.post(self.url, self.payload(), format="json").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/teams/locations/").status_code, 403)
        self.client.force_authenticate(self.manager)
        response = self.client.get("/api/v1/teams/locations/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertIsNone(response.data["results"][0]["latitude"])

    def test_rollback_discards_gps_event(self):
        with patch("realtime.events.get_channel_layer") as layer:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(RuntimeError), transaction.atomic():
                    update_location(actor=self.rescue, data=self.payload())
                    raise RuntimeError("rollback")
            layer.assert_not_called()
        self.team.refresh_from_db()
        self.assertIsNone(self.team.last_location)

    def test_redis_failure_does_not_turn_committed_command_into_failure(self):
        with patch("realtime.events.get_channel_layer") as layer:
            layer.return_value.group_send = AsyncMock(side_effect=ConnectionError("offline"))
            with self.assertLogs("realtime.events", level="WARNING") as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(self.url, self.payload(), format="json")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("105.02", str(logs.output))

    @override_settings(GPS_MIN_INTERVAL_SECONDS=60)
    def test_rate_limit_uses_server_time_and_is_shared_by_team_members(self):
        self.client.post(self.url, self.payload(), format="json")
        other_member = User.objects.create_user(username="member", role=Role.RESCUE_TEAM, response_team=self.team)
        self.client.force_authenticate(other_member)
        response = self.client.post(self.url, self.payload(timestamp=(timezone.now()+timedelta(seconds=20)).isoformat()), format="json")
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response["Retry-After"]), 50)
