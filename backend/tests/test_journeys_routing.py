import json
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlunsplit

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.db import transaction
from django.test import override_settings
from django.db import close_old_connections, connections
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.utils import timezone
from rest_framework.test import APITestCase, APITransactionTestCase

from accounts.models import Role, User
from dispatch.models import Assignment
from incidents.models import Incident, IncidentCategory
from teams.models import GPSJourney, GPSPoint, ResponseTeam
from teams.services import update_location
from teams import routing


class JourneyFixture:
    def setUp(self):
        self.now = timezone.now()
        self.team = ResponseTeam.objects.create(name="Synthetic team", code="journey-test")
        self.other = ResponseTeam.objects.create(name="Other team", code="other-journey")
        self.rescue = User.objects.create_user(username="journey-rescue", role=Role.RESCUE_TEAM, response_team=self.team)
        self.manager = User.objects.create_user(username="journey-manager", role=Role.DISPATCHER)
        self.category = IncidentCategory.objects.create(name="Synthetic", code="journey-test")
        self.incident = Incident.objects.create(title="Synthetic incident", category=self.category, location=Point(105.02, 21.01), status="verified")
        self.client.force_authenticate(self.rescue)

    def sample(self, seconds=0, **values):
        at = self.now + timedelta(seconds=seconds)
        with patch("teams.services.timezone.now", return_value=at):
            return update_location(actor=self.rescue, data={"latitude": 21, "longitude": 105 + seconds / 100000,
                "timestamp": at.isoformat(), "accuracy": 5, **values})


@override_settings(GPS_MIN_INTERVAL_SECONDS=1)
class JourneyTests(JourneyFixture, APITestCase):
    def test_persist_and_read_after_new_request_with_coordinates_time_accuracy(self):
        assignment = Assignment.objects.create(team=self.team, incident=self.incident, assigned_by=self.manager, dispatched_at=self.now)
        self.sample(); self.sample(10)
        journey = GPSJourney.objects.get()
        self.assertEqual(journey.assignment_id, assignment.pk)
        self.assertEqual(journey.points.count(), 2)
        # A fresh REST request reads the durable rows, not a client-side track buffer.
        params = {"since": (self.now - timedelta(seconds=1)).isoformat(), "until": (self.now + timedelta(minutes=1)).isoformat()}
        response = self.client.get(f"/api/v1/journeys/{journey.pk}/", params)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.data["segments"][0][0], [105, 21])
        self.assertEqual(response.data["points"][0]["accuracy"], 5)
        self.assertGreater(response.data["distance_meters"], 0)
        self.assertEqual(GPSPoint.objects.first().location.srid, 4326)

    def test_missing_accuracy_is_allowed(self):
        self.sample(accuracy=None)
        self.assertIsNone(GPSPoint.objects.get().accuracy)
        self.assertEqual(self.client.post("/api/v1/teams/me/location/", {"latitude": 21, "longitude": 105, "timestamp": self.now.isoformat()}, format="json").status_code, 409)

    def test_duplicate_rejected_without_duplicate_history_and_stationary_sample_kept(self):
        self.sample()
        with self.assertRaises(Exception) as caught:
            self.sample()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(GPSPoint.objects.count(), 1)
        self.sample(10, longitude=105)
        self.assertEqual(GPSPoint.objects.count(), 2)

    def test_gap_starts_new_journey_and_no_connector(self):
        self.sample(); self.sample(400)
        self.assertEqual(GPSJourney.objects.count(), 2)
        first = GPSJourney.objects.order_by("pk").first()
        self.assertEqual(first.ended_at, first.last_recorded_at)
        self.assertEqual(GPSJourney.objects.filter(ended_at__isnull=True).count(), 1)

    def test_assignment_change_starts_new_journey_without_inventing_old_points(self):
        self.sample()
        assignment = Assignment.objects.create(team=self.team, incident=self.incident, assigned_by=self.manager)
        self.sample(10)
        self.assertEqual(GPSJourney.objects.count(), 2)
        self.assertEqual(GPSJourney.objects.latest("pk").assignment_id, assignment.pk)
        self.assertEqual(GPSPoint.objects.count(), 2)

    def test_stop_idempotent_and_next_sample_starts_new_journey(self):
        self.sample()
        for _ in range(2):
            self.assertEqual(self.client.post("/api/v1/teams/me/journey/stop/", {}, format="json").status_code, 204)
        self.sample(10)
        self.assertEqual(GPSJourney.objects.count(), 2)

    def test_rollback_removes_point_and_journey_with_snapshot(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.sample()
            raise RuntimeError("synthetic rollback")
        self.assertFalse(GPSJourney.objects.exists())
        self.assertFalse(GPSPoint.objects.exists())
        self.team.refresh_from_db()
        self.assertIsNone(self.team.last_location)

    def test_roles_and_team_ownership_for_list_detail_stop(self):
        self.sample()
        foreign = GPSJourney.objects.create(team=self.other, started_at=self.now, last_recorded_at=self.now)
        response = self.client.get("/api/v1/journeys/")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(self.client.get(f"/api/v1/journeys/{foreign.pk}/").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/journeys/", {"team": self.other.pk}).data["count"], 0)
        for role in (Role.ADMIN, Role.DISPATCHER):
            actor = User.objects.create_user(username="history-" + role, role=role)
            self.client.force_authenticate(actor)
            self.assertEqual(self.client.get("/api/v1/journeys/").data["count"], 2)
            self.assertEqual(self.client.post("/api/v1/teams/me/journey/stop/", {}, format="json").status_code, 403)
        self.client.force_authenticate(User.objects.create_user(username="history-citizen"))
        self.assertEqual(self.client.get("/api/v1/journeys/").status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/journeys/").status_code, 401)

    def test_filters_limits_timezone_and_signal_lost(self):
        self.sample()
        invalid = [{"since": "invalid"}, {"since": "2026-01-01T00:00:00"}, {"team": -1},
                   {"since": (self.now - timedelta(days=40)).isoformat()}, {"until": (self.now - timedelta(days=40)).isoformat(), "since": self.now.isoformat()}]
        for params in invalid:
            self.assertEqual(self.client.get("/api/v1/journeys/", params).status_code, 400, params)
        with patch("teams.journeys.timezone.now", return_value=self.now + timedelta(minutes=20)):
            self.assertEqual(self.client.get("/api/v1/journeys/").data["results"][0]["tracking_state"], "signal_lost")
        self.sample(10)
        with override_settings(GPS_HISTORY_MAX_POINTS=1):
            params = {"until": (self.now + timedelta(minutes=1)).isoformat()}
            self.assertEqual(self.client.get(f"/api/v1/journeys/{GPSJourney.objects.get().pk}/", params).status_code, 400)

    def test_implausible_jump_splits_geometry_and_distance(self):
        self.sample(); self.sample(10, longitude=110)
        response = self.client.get(f"/api/v1/journeys/{GPSJourney.objects.get().pk}/", {"until": (self.now + timedelta(minutes=1)).isoformat()})
        self.assertEqual(len(response.data["segments"]), 2)
        self.assertEqual(response.data["distance_meters"], 0)

    def test_completed_assignment_keeps_own_team_history_but_not_live_state(self):
        assignment = Assignment.objects.create(team=self.team, incident=self.incident, assigned_by=self.manager, dispatched_at=self.now)
        self.sample()
        assignment.status, assignment.ended_at = "completed", timezone.now()
        assignment.save()
        response = self.client.get("/api/v1/journeys/")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["tracking_state"], "ended")
        self.assertEqual(response.data["results"][0]["assignment"], assignment.pk)

    def test_sample_before_dispatch_is_not_attached_to_future_assignment(self):
        Assignment.objects.create(team=self.team, incident=self.incident, assigned_by=self.manager, dispatched_at=self.now + timedelta(seconds=10))
        self.sample()
        self.assertIsNone(GPSJourney.objects.get().assignment_id)
        self.sample(20)
        self.assertEqual(GPSJourney.objects.count(), 2)


class JourneyConcurrencyTests(JourneyFixture, APITransactionTestCase):
    @patch("teams.services.publish_after_commit")
    def test_concurrent_repeated_sample_creates_one_journey_and_one_point(self, publish):
        barrier = Barrier(2)
        payload = {"latitude": 21, "longitude": 105, "timestamp": timezone.now().isoformat()}
        actor_id = self.rescue.pk
        def send():
            close_old_connections()
            try:
                actor = User.objects.get(pk=actor_id)
                barrier.wait(timeout=10)
                try:
                    update_location(actor=actor, data=payload)
                    return 200
                except Exception as error:
                    return getattr(error, "status_code", type(error).__name__)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: send(), range(2)))
        self.assertCountEqual(results, [200, 409])
        self.assertEqual(GPSJourney.objects.count(), 1)
        self.assertEqual(GPSPoint.objects.count(), 1)


@override_settings(GPS_MIN_INTERVAL_SECONDS=1, ROUTING_BASE_URL="https://routing.example.test", CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class RoutingTests(JourneyFixture, APITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client.force_authenticate(self.manager)
        self.payload = {"origin": {"longitude": 105, "latitude": 21}, "destination": {"longitude": 105.02, "latitude": 21.01}}
        self.provider_result = {"code": "Ok", "routes": [{"distance": 2500, "duration": 240, "geometry": {"type": "LineString", "coordinates": [[105, 21], [105.01, 21.005], [105.02, 21.01]]}}]}

    def upstream(self, opener, data=None):
        opener.return_value.open.return_value = BytesIO(json.dumps(self.provider_result if data is None else data).encode())

    @patch("teams.routing.build_opener")
    def test_route_distance_duration_arrival_geometry_and_cache(self, opener):
        self.upstream(opener)
        first = self.client.post("/api/v1/routes/", self.payload, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first["Cache-Control"], "no-store")
        self.assertEqual(first.data["distance_meters"], 2500)
        self.assertEqual(first.data["duration_seconds"], 240)
        self.assertFalse(first.data["traffic_aware"])
        self.assertEqual(first.data["estimated_arrival_at"] - first.data["calculated_at"], timedelta(seconds=240))
        request = opener.return_value.open.call_args.args[0]
        self.assertIn("/route/v1/driving/105.000000,21.000000;105.020000,21.010000", request.full_url)
        self.assertEqual(self.client.post("/api/v1/routes/", self.payload, format="json").status_code, 200)
        opener.return_value.open.assert_called_once()

    @patch("teams.routing.build_opener")
    def test_unconfigured_and_insecure_endpoint_never_calls_provider(self, opener):
        credential_endpoint = urlunsplit(("https", "user:pass@example.test", "", "", ""))
        for endpoint in ("", "http://localhost:5000", credential_endpoint, "https://example.test?key=bad"):
            with override_settings(ROUTING_BASE_URL=endpoint):
                response = self.client.post("/api/v1/routes/", self.payload, format="json")
                self.assertEqual(response.status_code, 503)
        opener.assert_not_called()

    @patch("teams.routing.build_opener")
    def test_no_route_and_malformed_provider_are_not_fake_estimates(self, opener):
        for data, expected in (({"code": "NoRoute"}, 422), ({"code": "NoSegment"}, 422), ({"code": "Ok", "routes": []}, 503),
                               ({"code": "Ok", "routes": [{"distance": -1}]}, 503)):
            cache.clear(); self.upstream(opener, data)
            response = self.client.post("/api/v1/routes/", self.payload, format="json")
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("duration_seconds", response.data)

    @patch("teams.routing.build_opener")
    def test_timeout_and_redis_failure_are_sanitized(self, opener):
        opener.return_value.open.side_effect = TimeoutError("secret provider URL")
        response = self.client.post("/api/v1/routes/", self.payload, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", str(response.data))
        with patch("teams.routing.cache.get", side_effect=ConnectionError("private Redis")):
            with self.assertRaises(routing.RoutingUnavailable):
                routing.route_between(actor=self.manager, **self.payload)

    @patch("teams.routing.build_opener")
    def test_http_error_no_segment_and_redirect_failure(self, opener):
        opener.return_value.open.side_effect = HTTPError("https://provider.invalid", 400, "bad", {}, BytesIO(b'{"code":"NoSegment"}'))
        self.assertEqual(self.client.post("/api/v1/routes/", self.payload, format="json").status_code, 422)
        cache.clear()
        opener.return_value.open.side_effect = HTTPError("https://provider.invalid", 302, "redirect", {}, None)
        self.assertEqual(self.client.post("/api/v1/routes/", self.payload, format="json").status_code, 503)

    def test_invalid_coordinate_and_role_permissions(self):
        for point in ({"latitude": 91, "longitude": 105}, {"latitude": "NaN", "longitude": 105}, {"latitude": True, "longitude": 105}):
            self.assertEqual(self.client.post("/api/v1/routes/", {**self.payload, "origin": point}, format="json").status_code, 400)
        self.client.force_authenticate(User.objects.create_user(username="route-citizen"))
        self.assertEqual(self.client.post("/api/v1/routes/", self.payload, format="json").status_code, 403)

    @patch("teams.routing.build_opener")
    def test_assignment_route_ownership_fresh_position_and_completed(self, opener):
        self.upstream(opener)
        assignment = Assignment.objects.create(team=self.team, incident=self.incident, assigned_by=self.manager)
        url = f"/api/v1/assignments/{assignment.pk}/route/"
        self.client.force_authenticate(self.rescue)
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 400)
        self.sample()
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 200)
        self.team.location_updated_at = self.now - timedelta(minutes=5)
        self.team.save()
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 400)
        other = User.objects.create_user(username="route-other", role=Role.RESCUE_TEAM, response_team=self.other)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 404)
        self.client.force_authenticate(self.rescue)
        assignment.status, assignment.ended_at = "completed", timezone.now()
        assignment.save()
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 400)

    @patch("teams.routing.build_opener")
    def test_invalid_geometry_bounds_and_numbers(self, opener):
        for coordinates in ([[105, 21]], [[181, 21], [105, 21]], [[True, 21], [105, 21]]):
            cache.clear()
            self.provider_result["routes"][0]["geometry"]["coordinates"] = coordinates
            self.upstream(opener)
            self.assertEqual(self.client.post("/api/v1/routes/", self.payload, format="json").status_code, 503)
