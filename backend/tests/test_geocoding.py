import json
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import Throttled
from rest_framework.test import APITestCase

from accounts.models import Role, User
from incidents import geocoding


class GeocodingServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def provider(self, data):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(data).encode()
        return response

    @patch("incidents.geocoding.urlopen")
    def test_reverse_formats_human_address_and_caches(self, opener):
        opener.return_value = self.provider({"lat": "21", "lon": "105", "address": {"house_number": "12", "road": "Synthetic Street", "suburb": "Synthetic Ward", "city": "Synthetic City"}})
        expected = {"address": "12 Synthetic Street, Synthetic Ward, Synthetic City"}
        self.assertEqual(geocoding.reverse(21, 105), expected)
        self.assertEqual(geocoding.reverse(21, 105), expected)
        opener.assert_called_once()
        request = opener.call_args.args[0]
        self.assertIn("EmergencyResponseSystem", request.get_header("User-agent"))
        self.assertNotIn("reporter", request.full_url)

    @patch("incidents.geocoding.urlopen")
    def test_shared_limit_blocks_second_upstream_request(self, opener):
        opener.return_value = self.provider([])
        self.assertEqual(geocoding.search("Synthetic Ward"), [])
        with self.assertRaises(Throttled):
            geocoding.search("Synthetic City")
        opener.assert_called_once()

    @patch("incidents.geocoding.urlopen")
    def test_provider_timeout_bad_json_and_cache_failure_are_safe(self, opener):
        for data in (b"not json", b"x" * 200001, b'{"lat":"NaN","lon":"105"}'):
            cache.clear()
            response = MagicMock(); response.__enter__.return_value.read.return_value = data
            opener.return_value = response
            with self.assertRaises(geocoding.GeocodingUnavailable):
                geocoding.reverse(21, 105)
        cache.clear(); opener.side_effect = TimeoutError("private provider URL")
        with self.assertRaises(geocoding.GeocodingUnavailable) as error:
            geocoding.reverse(21, 105)
        self.assertNotIn("private", str(error.exception))
        with patch("incidents.geocoding.cache.get", side_effect=ConnectionError("private Redis details")), self.assertRaises(geocoding.GeocodingUnavailable):
            geocoding.search("Synthetic Ward")

    @override_settings(GEOCODER_BASE_URL="https://configured-geocoder.invalid")
    @patch("incidents.geocoding.urlopen")
    def test_provider_can_be_switched_without_frontend_change(self, opener):
        opener.return_value = self.provider([])
        geocoding.search("Synthetic Ward")
        self.assertTrue(opener.call_args.args[0].full_url.startswith("https://configured-geocoder.invalid/search?"))


class GeocodingAPITests(APITestCase):
    def setUp(self):
        cache.clear()
        self.citizen = User.objects.create_user(username="synthetic-geocoder")
        self.client.force_authenticate(self.citizen)

    @patch("incidents.geocoding.search", return_value=[])
    def test_search_permissions_validation_and_empty_results(self, search):
        response = self.client.post("/api/v1/geocoding/search/", {"query": "Synthetic Ward"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])
        self.assertEqual(response["Cache-Control"], "no-store")
        for body in ({"query": "a"}, {"query": "Synthetic", "reporter_phone": "123"}):
            self.assertEqual(self.client.post("/api/v1/geocoding/search/", body, format="json").status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post("/api/v1/geocoding/search/", {"query": "Synthetic"}, format="json").status_code, 401)
        manager = User.objects.create_user(username="synthetic-manager", role=Role.DISPATCHER)
        self.client.force_authenticate(manager)
        self.assertEqual(self.client.post("/api/v1/geocoding/search/", {"query": "Synthetic"}, format="json").status_code, 403)

    @patch("incidents.geocoding.reverse", side_effect=geocoding.GeocodingUnavailable())
    def test_reverse_failure_is_a_recoverable_503_and_bad_coordinates_are_400(self, reverse):
        self.assertEqual(self.client.post("/api/v1/geocoding/reverse/", {"latitude": 21, "longitude": 105}, format="json").status_code, 503)
        for coordinate in (91, "NaN", True):
            self.assertEqual(self.client.post("/api/v1/geocoding/reverse/", {"latitude": coordinate, "longitude": 105}, format="json").status_code, 400)
