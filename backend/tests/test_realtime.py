from contextlib import asynccontextmanager
from uuid import uuid4

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from accounts.models import Role, User
from config.asgi import application
from dispatch.services import assign_team, cancel_assignment, reassign_team, transition_assignment, submit_signal
from incidents.models import Incident, IncidentCategory
from incidents.services import change_incident_status, submit_report, review_report, link_reports
from teams.models import ResponseTeam
from teams.services import update_location

# Exercise the real Redis transport, isolated from development socket groups.
TEST_LAYERS = {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {
    **settings.CHANNEL_LAYERS["default"]["CONFIG"], "prefix": f"test.phase5.{uuid4().hex}",
}}}


@override_settings(CHANNEL_LAYERS=TEST_LAYERS)
class RealtimeTests(TransactionTestCase):
    def setUp(self):
        self.team = ResponseTeam.objects.create(name="Synthetic", code="rt", status="available")
        self.other = ResponseTeam.objects.create(name="Other", code="other", status="available")
        self.manager = User.objects.create_user(username="manager", role=Role.DISPATCHER)
        self.rescue = User.objects.create_user(username="rescue", role=Role.RESCUE_TEAM, response_team=self.team)
        self.outsider = User.objects.create_user(username="other", role=Role.RESCUE_TEAM, response_team=self.other)
        self.citizen = User.objects.create_user(username="citizen")
        self.admin = User.objects.create_user(username="admin", role=Role.ADMIN)
        self.tokens = {u.pk: Token.objects.create(user=u).key for u in [self.manager, self.rescue, self.outsider, self.citizen, self.admin]}
        self.category = IncidentCategory.objects.create(code="synthetic", name="Synthetic")
        self.team.categories.add(self.category)
        self.other.categories.add(self.category)
        self.incident = Incident.objects.create(title="Synthetic", category=self.category, location=Point(105,21,srid=4326), status="verified")

    def communicator(self, path="/ws/dispatcher/", origin=b"http://localhost:8000"):
        return WebsocketCommunicator(application, path, headers=[(b"origin", origin)])

    @asynccontextmanager
    async def connected(self, user, path="/ws/dispatcher/"):
        client = self.communicator(path)
        try:
            self.assertTrue((await client.connect())[0])
            await client.send_json_to({"type": "authenticate", "token": self.tokens[user.pk]})
            self.assertEqual((await client.receive_json_from())["type"], "ready")
            yield client
        finally:
            await client.disconnect()

    async def test_connections_ping_disconnect_and_reconnect(self):
        for user, path in [(self.manager,"/ws/dispatcher/"), (self.admin,"/ws/dispatcher/"), (self.rescue,"/ws/rescue/")]:
            for _ in range(2):
                async with self.connected(user,path) as client:
                    await client.send_json_to({"type":"ping"})
                    self.assertEqual((await client.receive_json_from())["type"], "pong")

    async def test_field_signal_event_only_dispatchers_no_contact_or_note(self):
        assignment = await database_sync_to_async(assign_team)(actor=self.manager, incident_id=self.incident.pk, team_id=self.team.pk)
        async with self.connected(self.manager) as manager, self.connected(self.rescue, "/ws/rescue/") as rescue:
            await database_sync_to_async(submit_signal)(actor=self.rescue, assignment_id=assignment.pk, data={
                "request_id": str(uuid4()), "kind": "support", "support_type": "medical", "note": "Synthetic private detail",
                "latitude": 21, "longitude": 105, "reported_at": timezone.now().isoformat()})
            event = await manager.receive_json_from()
            self.assertEqual(event["type"], "assignment.signal_created")
            self.assertEqual(set(event["data"]), {"signal_id", "assignment_id", "incident_id", "team_id", "kind"})
            self.assertTrue(await rescue.receive_nothing(timeout=0.1))

    async def test_report_event_does_not_broadcast_contact_values(self):
        async with self.connected(self.manager) as client:
            await database_sync_to_async(submit_report)(actor=self.citizen, category=self.category,
                description="Synthetic", location=Point(105, 21, srid=4326), reporter_name="Synthetic name",
                reporter_phone="+84900000000", allow_contact=True)
            event = await client.receive_json_from()
            self.assertEqual(event["type"], "report.changed")
            self.assertEqual(set(event["data"]), {"report_id", "review_status", "incident_id", "updated_at"})

    async def test_idle_socket_survives_redis_blocking_receive_timeout(self):
        async with self.connected(self.manager) as client:
            # Redis channel receive blocks for 5 seconds between empty polls.
            self.assertTrue(await client.receive_nothing(timeout=5.5))
            await client.send_json_to({"type": "ping"})
            self.assertEqual((await client.receive_json_from())["type"], "pong")

    async def test_invalid_role_or_token_cannot_authenticate(self):
        for user, path in [(self.citizen,"/ws/dispatcher/"), (self.rescue,"/ws/dispatcher/"), (self.manager,"/ws/rescue/")]:
            client = self.communicator(path)
            try:
                await client.connect()
                await client.send_json_to({"type":"authenticate", "token":self.tokens[user.pk]})
                self.assertEqual((await client.receive_output())["code"], 4403)
            finally:
                await client.disconnect()
        client = self.communicator()
        try:
            await client.connect()
            await client.send_json_to({"type":"authenticate", "token":"x"*40})
            self.assertEqual((await client.receive_output())["code"], 4401)
        finally:
            await client.disconnect()

    async def test_missing_membership_and_inactive_user_denied(self):
        await database_sync_to_async(User.objects.filter(pk=self.rescue.pk).update)(response_team=None)
        client = self.communicator("/ws/rescue/")
        try:
            await client.connect()
            await client.send_json_to({"type":"authenticate", "token":self.tokens[self.rescue.pk]})
            self.assertEqual((await client.receive_output())["code"], 4403)
        finally:
            await client.disconnect()
        await database_sync_to_async(User.objects.filter(pk=self.manager.pk).update)(is_active=False)
        client = self.communicator()
        try:
            await client.connect()
            await client.send_json_to({"type":"authenticate", "token":self.tokens[self.manager.pk]})
            self.assertEqual((await client.receive_output())["code"], 4401)
        finally:
            await client.disconnect()

    async def test_bad_origin_rejected_before_connection(self):
        client = self.communicator(origin=b"https://untrusted.example")
        self.assertFalse((await client.connect())[0])
        await client.disconnect()

    @override_settings(WS_AUTH_TIMEOUT_SECONDS=0.05)
    async def test_anonymous_connection_times_out_even_with_query_token(self):
        client = self.communicator(f"/ws/dispatcher/?token={self.tokens[self.manager.pk]}")
        try:
            await client.connect()
            self.assertEqual((await client.receive_output())["code"], 4401)
        finally:
            await client.disconnect()

    async def test_malformed_binary_oversize_messages_close_safely(self):
        for value in ("{", "[]", "x"*2049):
            client = self.communicator()
            try:
                await client.connect()
                await client.send_to(text_data=value)
                self.assertEqual((await client.receive_output())["code"], 4400)
            finally:
                await client.disconnect()
        client = self.communicator()
        try:
            await client.connect()
            await client.send_to(bytes_data=b"binary")
            self.assertEqual((await client.receive_output())["code"], 4400)
        finally:
            await client.disconnect()

    async def test_group_injection_and_websocket_commands_rejected(self):
        async with self.connected(self.rescue,"/ws/rescue/") as client:
            for message in ({"type":"subscribe","group":"dispatchers"}, {"type":"gps","latitude":1}, {"type":"assignment.accept","id":1}):
                await client.send_json_to(message)
                self.assertEqual((await client.receive_json_from())["code"], "unsupported_message")
            await get_channel_layer().group_send("dispatchers", {"type":"domain.event","payload":{"type":"private"}})
            self.assertTrue(await client.receive_nothing(timeout=0.1))

    async def test_logout_or_team_change_revokes_before_next_event(self):
        async with self.connected(self.rescue,"/ws/rescue/") as client:
            await database_sync_to_async(User.objects.filter(pk=self.rescue.pk).update)(response_team=self.other)
            await get_channel_layer().group_send(f"team.{self.team.pk}", {"type":"domain.event","payload":{"type":"private"}})
            self.assertEqual((await client.receive_output())["code"], 4403)
        async with self.connected(self.manager) as client:
            await database_sync_to_async(Token.objects.filter(user=self.manager).delete)()
            await client.send_json_to({"type":"ping"})
            self.assertEqual((await client.receive_output())["code"], 4403)

    @override_settings(WS_AUTH_TIMEOUT_SECONDS=0.5, WS_HEARTBEAT_SECONDS=0.05)
    async def test_idle_permission_revocation_detected_by_watchdog(self):
        async with self.connected(self.manager) as client:
            await database_sync_to_async(User.objects.filter(pk=self.manager.pk).update)(role=Role.CITIZEN)
            result = await client.receive_output()
            if result["type"] == "websocket.send":
                result = await client.receive_output()
            self.assertEqual(result["code"], 4403)

    async def test_incident_service_broadcasts_minimal_data_only_to_dispatchers(self):
        async with self.connected(self.manager) as manager, self.connected(self.rescue,"/ws/rescue/") as rescue:
            await database_sync_to_async(change_incident_status)(actor=self.manager, incident_id=self.incident.pk, status="in_progress", expected_status="verified")
            event = await manager.receive_json_from()
            self.assertEqual(event["type"], "incident.status_changed")
            self.assertEqual(event["data"]["status"], "in_progress")
            self.assertEqual(set(event["data"]), {"incident_id","status","updated_at"})
            self.assertTrue(await rescue.receive_nothing(timeout=0.1))

    async def test_new_report_and_review_reach_dispatchers_after_commit_only(self):
        async with self.connected(self.manager) as manager, self.connected(self.rescue, "/ws/rescue/") as rescue:
            report = await database_sync_to_async(submit_report)(actor=self.citizen, category=self.category, description="Synthetic", location=Point(105, 21, srid=4326))
            event = await manager.receive_json_from()
            self.assertEqual(event["type"], "report.changed")
            self.assertEqual(set(event["data"]), {"report_id", "review_status", "incident_id", "updated_at"})
            await database_sync_to_async(review_report)(actor=self.manager, report_id=report.pk, review_status="accepted")
            self.assertEqual((await manager.receive_json_from())["data"]["review_status"], "accepted")
            self.assertTrue(await rescue.receive_nothing(timeout=0.1))

    async def test_link_reports_emits_one_batch_event_and_no_event_for_noop(self):
        from incidents.models import IncidentReport
        report = await database_sync_to_async(IncidentReport.objects.create)(reporter=self.citizen, category=self.category, description="Synthetic", location=Point(105, 21, srid=4326), review_status="accepted")
        async with self.connected(self.manager) as manager, self.connected(self.rescue, "/ws/rescue/") as rescue:
            await database_sync_to_async(link_reports)(actor=self.manager, incident_id=self.incident.pk, report_ids=[report.pk])
            event = await manager.receive_json_from()
            self.assertEqual(event["type"], "reports.linked")
            self.assertEqual(event["data"], {"incident_id": self.incident.pk, "report_ids": [report.pk]})
            await database_sync_to_async(link_reports)(actor=self.manager, incident_id=self.incident.pk, report_ids=[report.pk])
            self.assertTrue(await manager.receive_nothing(timeout=0.1))
            self.assertTrue(await rescue.receive_nothing(timeout=0.1))

    async def test_assignment_events_are_scoped_and_cover_full_lifecycle(self):
        async with self.connected(self.manager) as manager, self.connected(self.rescue,"/ws/rescue/") as rescue, self.connected(self.outsider,"/ws/rescue/") as outsider:
            assignment = await database_sync_to_async(assign_team)(actor=self.manager, incident_id=self.incident.pk, team_id=self.team.pk)
            self.assertEqual((await manager.receive_json_from())["data"]["status"], "assigned")
            self.assertEqual((await manager.receive_json_from())["type"], "incident.status_changed")
            self.assertEqual((await rescue.receive_json_from())["data"]["status"], "assigned")
            for previous,target in [("pending","accepted"),("accepted","en_route"),("en_route","on_scene"),("on_scene","responding"),("responding","completed")]:
                await database_sync_to_async(transition_assignment)(actor=self.rescue, assignment_id=assignment.pk, status=target, expected_status=previous)
                event = await manager.receive_json_from()
                self.assertEqual(event["type"], "assignment.status_changed")
                self.assertEqual((await rescue.receive_json_from())["data"]["status"], target)
                self.assertNotIn("note", event["data"])
                if target == "on_scene":
                    self.assertEqual((await manager.receive_json_from())["data"]["status"], "in_progress")
            self.assertTrue(await outsider.receive_nothing(timeout=0.1))

    async def test_reassign_notifies_old_and_new_teams_and_cancel_notifies_new_team(self):
        assignment = await database_sync_to_async(assign_team)(actor=self.manager, incident_id=self.incident.pk, team_id=self.team.pk)
        async with self.connected(self.rescue,"/ws/rescue/") as old, self.connected(self.outsider,"/ws/rescue/") as new:
            replacement = await database_sync_to_async(reassign_team)(actor=self.manager, assignment_id=assignment.pk, team_id=self.other.pk, expected_status="pending")
            self.assertEqual((await old.receive_json_from())["data"]["status"], "cancelled")
            event = await new.receive_json_from()
            self.assertEqual(event["data"]["supersedes"], assignment.pk)
            await database_sync_to_async(cancel_assignment)(actor=self.manager, assignment_id=replacement.pk, expected_status="pending")
            self.assertEqual((await new.receive_json_from())["data"]["status"], "cancelled")
            self.assertTrue(await old.receive_nothing(timeout=0.1))

    async def test_gps_live_event_only_to_dispatchers(self):
        async with self.connected(self.manager) as manager, self.connected(self.rescue,"/ws/rescue/") as rescue:
            await database_sync_to_async(update_location)(actor=self.rescue, data={"latitude":21,"longitude":105,"accuracy":4,"timestamp":timezone.now().isoformat()})
            event = await manager.receive_json_from()
            self.assertEqual(event["type"], "team.location_updated")
            self.assertEqual(event["data"]["longitude"], 105)
            self.assertTrue(await rescue.receive_nothing(timeout=0.1))

    async def test_rollback_and_same_state_do_not_emit_and_reconnect_recovers_snapshot(self):
        def rollback():
            try:
                with transaction.atomic():
                    change_incident_status(actor=self.manager, incident_id=self.incident.pk, status="in_progress", expected_status="verified")
                    raise RuntimeError("abort")
            except RuntimeError:
                pass

        async with self.connected(self.manager) as client:
            await database_sync_to_async(rollback)()
            await database_sync_to_async(change_incident_status)(actor=self.manager, incident_id=self.incident.pk, status="verified", expected_status="verified")
            self.assertTrue(await client.receive_nothing(timeout=0.1))
        await database_sync_to_async(change_incident_status)(actor=self.manager, incident_id=self.incident.pk, status="in_progress", expected_status="verified")
        async with self.connected(self.manager) as client:
            self.assertTrue(await client.receive_nothing(timeout=0.1))  # No replay of disconnected events.
            incident = await database_sync_to_async(Incident.objects.get)(pk=self.incident.pk)
            self.assertEqual(incident.status, "in_progress")  # REST snapshot remains authoritative.
