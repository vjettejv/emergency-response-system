import io
import json
import logging
import os
import runpy
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from redis import ConnectionError as RedisConnectionError

from common.logging import SafeFormatter
from incidents.models import IncidentCategory


class ProductionSettingsTests(SimpleTestCase):
    def config(self, **changes):
        environment = {
            "DJANGO_ENV": "production", "DJANGO_SECRET_KEY": "synthetic-test-only-" + "x" * 60,
            "DJANGO_DEBUG": "false", "DJANGO_ALLOWED_HOSTS": "emergency.example.com",
            "WS_ALLOWED_ORIGINS": "https://emergency.example.com", "POSTGRES_PASSWORD": "synthetic",
            "POSTGRES_HOST": "synthetic.rds.amazonaws.com", "POSTGRES_SSLROOTCERT": "/synthetic/ca.pem",
            **changes,
        }
        with patch.dict(os.environ, environment, clear=True):
            return runpy.run_path(str(Path(__file__).resolve().parents[1] / "config/settings.py"))

    def test_production_tls_database_cache_and_forwarding(self):
        config = self.config()
        self.assertFalse(config["DEBUG"])
        self.assertTrue(config["SECURE_SSL_REDIRECT"])
        self.assertTrue(config["SESSION_COOKIE_SECURE"])
        self.assertTrue(config["CSRF_COOKIE_SECURE"])
        self.assertEqual(config["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertEqual(config["DATABASES"]["default"]["OPTIONS"]["sslmode"], "verify-full")
        self.assertIn("redis.RedisCache", config["CACHES"]["default"]["BACKEND"])
        self.assertEqual(config["REST_FRAMEWORK"]["NUM_PROXIES"], 1)
        self.assertTrue(config["CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP"])

    def test_insecure_production_configuration_fails_without_echoing_values(self):
        from django.core.exceptions import ImproperlyConfigured
        for changes in ({"DJANGO_ENV": "prod-typo"}, {"DJANGO_DEBUG": "true"}, {"DJANGO_SECRET_KEY": "synthetic-short"},
                        {"DJANGO_ALLOWED_HOSTS": "*"}, {"DJANGO_ALLOWED_HOSTS": ".example.com"},
                        {"WS_ALLOWED_ORIGINS": "*"}, {"WS_ALLOWED_ORIGINS": ""},
                        {"CSRF_TRUSTED_ORIGINS": "http://emergency.example.com"},
                        {"CORS_ALLOWED_ORIGINS": "https://example.com/path"},
                        {"POSTGRES_SSLMODE": "require"}, {"POSTGRES_SSLROOTCERT": ""}):
            with self.subTest(changes=list(changes)), self.assertRaises(ImproperlyConfigured):
                self.config(**changes)

    def test_http_bootstrap_does_not_require_domain_or_weaken_rds_tls(self):
        config = self.config(DJANGO_ALLOWED_HOSTS="192.0.2.1", DJANGO_HTTPS_ENABLED="false",
                             WS_ALLOWED_ORIGINS="http://192.0.2.1")
        self.assertFalse(config["SECURE_SSL_REDIRECT"])
        self.assertEqual(config["DATABASES"]["default"]["OPTIONS"]["sslmode"], "verify-full")


class HealthAndCorsTests(TestCase):
    def test_live_database_readiness_and_no_infrastructure_leak(self):
        with patch("common.health.Redis.from_url") as redis:
            redis.return_value.__enter__.return_value.ping.return_value = True
            response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "ok", "redis": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_dependency_failure_reports_503_without_exception_details(self):
        for database_down in (False, True):
            with patch("common.health.Redis.from_url", side_effect=RedisConnectionError("synthetic-private-url")):
                if database_down:
                    with patch("common.health.connection.cursor", side_effect=OperationalError("synthetic-password")):
                        response = self.client.get("/api/health/")
                else:
                    response = self.client.get("/api/health/")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("synthetic", response.content.decode())
            self.assertEqual(response.json()["database"], "unavailable" if database_down else "ok")

    @override_settings(SECURE_SSL_REDIRECT=True, SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
    def test_https_proxy_and_internal_probe(self):
        with patch("common.health.Redis.from_url") as redis:
            redis.return_value.__enter__.return_value.ping.return_value = True
            self.assertEqual(self.client.get("/api/health/").status_code, 200)
        self.assertEqual(self.client.get("/realtime/").status_code, 301)
        self.assertEqual(self.client.get("/realtime/", HTTP_X_FORWARDED_PROTO="https").status_code, 200)

    @override_settings(CORS_ALLOWED_ORIGINS=["https://client.example.com"])
    def test_cors_exact_origin_preflight_and_auth_still_required(self):
        response = self.client.options("/api/v1/auth/me/", HTTP_ORIGIN="https://client.example.com",
                                      HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
                                      HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization, content-type")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://client.example.com")
        self.assertNotIn("Access-Control-Allow-Credentials", response)
        response = self.client.get("/api/v1/auth/me/", HTTP_ORIGIN="https://client.example.com")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Origin", response["Vary"])
        for origin, headers in (("https://evil.example.com", "authorization"),
                                ("https://client.example.com", "x-unsafe")):
            response = self.client.options("/api/v1/auth/me/", HTTP_ORIGIN=origin,
                                          HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
                                          HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("Access-Control-Allow-Origin", response)

    def test_root_keeps_existing_frontend_route(self):
        self.assertRedirects(self.client.get("/"), "/realtime/", fetch_redirect_response=False)


class DeploymentCommandsTests(TestCase):
    def test_category_seed_is_idempotent_and_preserves_records(self):
        IncidentCategory.objects.create(code="fire", name="Locally configured name", is_active=False)
        call_command("seed_categories", stdout=io.StringIO())
        call_command("seed_categories", stdout=io.StringIO())
        self.assertEqual(IncidentCategory.objects.count(), 5)
        fire = IncidentCategory.objects.get(code="fire")
        self.assertEqual(fire.name, "Locally configured name")
        self.assertFalse(fire.is_active)

    def test_read_only_infrastructure_command_runs_spatial_sql(self):
        output = io.StringIO()
        with patch("common.management.commands.check_infrastructure.get_channel_layer") as layers, \
                patch("common.management.commands.check_infrastructure.app") as celery, \
                patch("common.management.commands.check_infrastructure.s3.check_bucket") as bucket:
            layers.return_value = None  # Replace the async call rather than a real live group.
            with patch("common.management.commands.check_infrastructure.async_to_sync", return_value=MagicMock()):
                layers.return_value = MagicMock(spec=["group_send"])
                celery.control.inspect.return_value.ping.return_value = {"synthetic": {"ok": "pong"}}
                call_command("check_infrastructure", s3=True, stdout=output)
            bucket.assert_called_once()
        self.assertIn("PostGIS spatial query: ok", output.getvalue())
        self.assertIn("upload not tested", output.getvalue())

    def test_worker_failure_is_sanitized_and_fails_command(self):
        with patch("common.management.commands.check_infrastructure.get_channel_layer") as layer, \
                patch("common.management.commands.check_infrastructure.async_to_sync", return_value=MagicMock()), \
                patch("common.management.commands.check_infrastructure.app") as celery:
            layer.return_value = MagicMock(spec=["group_send"])
            celery.connection_for_read.side_effect = RuntimeError("synthetic-token-and-internal-url")
            with self.assertRaisesMessage(CommandError, "Redis/channel/broker/worker check failed."):
                call_command("check_infrastructure", stdout=io.StringIO())


class LoggingPrivacyTests(SimpleTestCase):
    def test_framework_payloads_and_exception_values_are_not_logged(self):
        for name in ("django.request", "daphne.http_protocol", "celery.app.trace", "botocore"):
            record = logging.LogRecord(name, logging.ERROR, __file__, 1, "private %s", ("synthetic-secret",),
                                       (ValueError, ValueError("synthetic-phone"), None))
            record.status_code = 500
            formatted = SafeFormatter().format(record)
            self.assertNotIn("synthetic", formatted)
            self.assertIn("error=ValueError", formatted)
            self.assertIn("status=500", formatted)
