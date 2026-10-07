"""Opt-in local release measurements: manage.py test tests.release_benchmark.

Uses a disposable Django test database. Not discovered by the ordinary test_*
suite; no production records, S3 calls or additional load-test dependencies.
"""
import asyncio
import io
import json
import math
import os
from pathlib import Path
import platform
import resource
import socket
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Role, User
from dispatch.models import Assignment
from dispatch.suggestions import eligible_team_query
from evidence.images import optimize_image
from incidents.clustering import candidate_queries
from incidents.models import Incident, IncidentCategory, IncidentReport
from notifications.models import Notification
from teams.models import ResponseTeam
from config.asgi import application


def metric(samples, elapsed=None):
    values = sorted(samples)
    percentile = lambda fraction: round(values[max(0, math.ceil(len(values) * fraction) - 1)], 3)
    return {"samples": len(values), "avg_ms": round(statistics.mean(values), 3),
            "p50_ms": percentile(.5), "p95_ms": percentile(.95), "p99_ms": percentile(.99),
            **({"throughput_rps": round(len(values) / elapsed, 2)} if elapsed else {})}


def emit(kind, data):
    print("RELEASE_BENCHMARK " + json.dumps({"kind": kind, **data}, sort_keys=True), flush=True)


class ReleaseBenchmark(TransactionTestCase):
    def setUp(self):
        self.category = IncidentCategory.objects.create(code="bench", name="Synthetic benchmark")
        self.citizen = User.objects.create_user(username="bench-citizen", password="Synthetic-benchmark-only-42!")
        self.manager = User.objects.create_user(username="bench-manager", role=Role.DISPATCHER)
        self.token = Token.objects.create(user=self.manager).key
        self.citizen_token = Token.objects.create(user=self.citizen).key
        now = timezone.now()
        IncidentReport.objects.bulk_create([IncidentReport(reporter=self.citizen, category=self.category,
            description="Synthetic benchmark", occurred_at=now,
            location=Point(106.7 if i < 20 else 108 + (i % 100) * .01, 10.77 + (i // 100) * .01, srid=4326))
            for i in range(2000)])
        Incident.objects.bulk_create([Incident(title="Synthetic benchmark", category=self.category,
            location=Point(106.7 + (i % 100) * .01, 10.77 + (i // 100) * .01, srid=4326), status="verified")
            for i in range(500)])
        ResponseTeam.objects.bulk_create([ResponseTeam(code=f"bench-{i}", name="Synthetic benchmark", status="available",
            last_location=Point(106.7 + (i % 100) * .01, 10.77 + (i // 100) * .01, srid=4326)) for i in range(300)])
        through = ResponseTeam.categories.through
        through.objects.bulk_create([through(responseteam_id=pk, incidentcategory_id=self.category.pk)
                                     for pk in ResponseTeam.objects.values_list("pk", flat=True)])
        self.incident = Incident.objects.first()
        self.report = IncidentReport.objects.first()
        self.team = ResponseTeam.objects.first()
        from datetime import timedelta
        Assignment.objects.bulk_create([Assignment(incident=self.incident, team=self.team, assigned_by=self.manager,
            status="completed", ended_at=now + timedelta(minutes=5)) for _ in range(20)])
        Notification.objects.bulk_create([Notification(recipient=self.manager, type="new_report", title="Synthetic",
            message="Synthetic", event_key=f"bench:{i}") for i in range(100)])
        with connection.cursor() as cursor:
            for table in ("incidents_incidentreport", "incidents_incident", "teams_responseteam", "dispatch_assignment"):
                cursor.execute("ANALYZE " + table)
            cursor.execute("SELECT version(), PostGIS_Lib_Version()")
            database, postgis = cursor.fetchone()
        emit("environment", {"python": platform.python_version(), "database": database, "postgis": postgis,
             "cpu_visible": os.cpu_count(), "reports": 2000, "incidents": 500, "teams": 300,
             "assignments": 20, "notifications": 100, "transport": "Docker local loopback"})

    def test_rest_and_spatial(self):
        # Unforced optimizer plans: retain the actual index selection, no SLA assertion.
        for name, query in (("duplicate_reports", candidate_queries(self.report)[0]),
                            ("nearby_teams", eligible_team_query(self.incident))):
            samples = []
            for _ in range(30):
                started = time.perf_counter()
                list(query.all()[:20])
                samples.append((time.perf_counter() - started) * 1000)
            plan = json.loads(query[:20].explain(format="json", analyze=True, buffers=True))[0]
            emit("spatial", {"name": name, **metric(samples), "plan": plan,
                 "radius_meters": settings.CLUSTER_RADIUS_METERS if name == "duplicate_reports" else settings.DISPATCH_RADIUS_METERS})

        client = APIClient(); client.force_authenticate(self.manager)
        for path in ("incident-reports/", "incidents/", f"incidents/{self.incident.pk}/",
                     f"incidents/{self.incident.pk}/suggested-teams/", "assignments/", "notifications/"):
            with CaptureQueriesContext(connection) as queries:
                response = client.get("/api/v1/" + path)
                self.assertEqual(response.status_code, 200)
            emit("orm", {"endpoint": path.split("/")[0], "queries": len(queries), "page_size": 20})

        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0)); port = port_socket.getsockname()[1]
        environment = {**os.environ, "POSTGRES_DB": connection.settings_dict["NAME"],
            "REDIS_CHANNEL_PREFIX": settings.CHANNEL_LAYERS["default"]["CONFIG"]["prefix"],
            "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver"}
        server = subprocess.Popen([sys.executable, "-m", "daphne", "-b", "127.0.0.1", "-p", str(port),
            "--access-log", "/dev/null", "config.asgi:application"], env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.assertIsNone(server.poll(), "Temporary Daphne exited before readiness")
                try:
                    with urlopen(base + "/", timeout=1) as response:
                        if response.status == 200: break
                except OSError:
                    time.sleep(.1)
            else:
                self.fail("Temporary Daphne did not become ready")

            def request(path, method="GET", body=None, token=None):
                headers = {"Content-Type": "application/json"}
                if token: headers["Authorization"] = "Token " + token
                started = time.perf_counter()
                try:
                    with urlopen(Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers, method=method), timeout=15) as response:
                        result, status = json.loads(response.read()), response.status
                except HTTPError as error:
                    error.read(); result, status = {}, error.code
                return (time.perf_counter() - started) * 1000, status, result

            groups = [
                ("auth", "/api/v1/auth/login/", "POST", {"username": self.citizen.username, "password": "Synthetic-benchmark-only-42!"}, None, 10, 1),
                ("incident_list", "/api/v1/incidents/", "GET", None, self.token, 100, 4),
                ("incident_detail", f"/api/v1/incidents/{self.incident.pk}/", "GET", None, self.token, 100, 4),
                ("duplicates", f"/api/v1/incident-reports/{self.report.pk}/potential-duplicates/", "GET", None, self.token, 60, 4),
                ("suggestions", f"/api/v1/incidents/{self.incident.pk}/suggested-teams/", "GET", None, self.token, 60, 4),
                ("report_create", "/api/v1/incident-reports/", "POST", {"category": self.category.pk,
                    "description": "Synthetic benchmark", "latitude": 10.77, "longitude": 106.7,
                    "reporter_name": "Synthetic", "reporter_phone": "+12025550123"}, self.citizen_token, 20, 4),
            ]
            for name, path, method, body, token, count, workers in groups:
                started = time.perf_counter()
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    results = list(pool.map(lambda _: request(path, method, body, token), range(count)))
                elapsed = time.perf_counter() - started
                errors = sum(status >= 400 for _, status, _ in results)
                emit("rest", {"name": name, "workers": workers, "errors": errors,
                     "error_pct": 100 * errors / count, "duration_seconds": round(elapsed, 3),
                     **metric([latency for latency, _, _ in results], elapsed)})
                self.assertEqual(errors, 0, name)

            latencies = []
            for team in ResponseTeam.objects.all()[:20]:
                latency, status, result = request(f"/api/v1/incidents/{self.incident.pk}/assignments/", "POST", {"team_id": team.pk}, self.token)
                self.assertEqual(status, 201)
                latencies.append(latency)
            emit("rest", {"name": "dispatch", "workers": 1, "errors": 0, "error_pct": 0, **metric(latencies)})
        finally:
            server.terminate()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait(timeout=5)

    async def test_websocket_fanout(self):
        clients = [WebsocketCommunicator(application, "/ws/dispatcher/", headers=[(b"origin", b"http://localhost:8000")]) for _ in range(40)]
        layer = get_channel_layer()
        connection_latencies, deliveries = [], []
        async def connect(client):
            started = time.perf_counter()
            self.assertTrue((await client.connect(timeout=10))[0])
            await client.send_json_to({"type": "authenticate", "token": self.token})
            self.assertEqual((await client.receive_json_from(timeout=10))["type"], "ready")
            connection_latencies.append((time.perf_counter() - started) * 1000)
        try:
            await asyncio.gather(*(connect(client) for client in clients))
            for index in range(20):
                kind = "team.location_updated" if index % 2 else "incident.status_changed"
                payload = {"type": kind, "data": {"sequence": index}}
                started = time.perf_counter()
                await layer.group_send("dispatchers", {"type": "domain.event", "payload": payload})
                async def receive(client):
                    event = await client.receive_json_from(timeout=10)
                    self.assertEqual(event, payload)
                    deliveries.append((time.perf_counter() - started) * 1000)
                await asyncio.gather(*(receive(client) for client in clients))
            emit("websocket", {"transport": "ASGI communicator + real Redis (no network/TLS)",
                 "connections": 40, "failures": 0, "events": 20, "deliveries": len(deliveries),
                 "connection": metric(connection_latencies), "fanout_latency": metric(deliveries),
                 "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
        finally:
            await asyncio.gather(*(client.disconnect(timeout=5) for client in clients))

    def test_images(self):
        from PIL import Image, ImageOps
        from tests.test_image_processing import image_bytes
        sources = [("synthetic_noise_jpeg", image_bytes(size=(2400, 1800), noise=True), "image/jpeg")]
        original_path = Path(os.environ.get("RELEASE_IMAGE_PATH", "/bench-images/landscape-iphone.jpg"))
        if original_path.is_file():
            original = original_path.read_bytes()
            sources.append(("public_iphone_xs_landscape", original, "image/jpeg"))
            with Image.open(io.BytesIO(original)) as image:
                photo = ImageOps.exif_transpose(image).convert("RGB")
                portrait = photo.crop((1000, 0, 3000, 3024))
                buffer = io.BytesIO(); portrait.save(buffer, format="JPEG", quality=95)
                sources.append(("portrait_crop_from_public_photo", buffer.getvalue(), "image/jpeg"))
                buffer = io.BytesIO(); photo.save(buffer, format="PNG")
                sources.append(("png_conversion_from_public_photo", buffer.getvalue(), "image/png"))
        for name, data, mime in sources:
            samples = []
            for _ in range(5):
                started = time.perf_counter(); output, _, width, height = optimize_image(data, mime)
                samples.append((time.perf_counter() - started) * 1000)
            emit("image", {"name": name, "original_bytes": len(data), "optimized_bytes": len(output),
                 "reduction_pct": round(100 * (1 - len(output) / len(data)), 2), "width": width, "height": height,
                 "max_dimension": settings.MEDIA_IMAGE_MAX_DIMENSION, "quality": settings.MEDIA_IMAGE_QUALITY, **metric(samples)})
