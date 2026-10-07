from unittest.mock import AsyncMock, patch
from channels.testing import WebsocketCommunicator
from django.db import OperationalError
from django.test import TransactionTestCase
from config.asgi import application
from channels.layers import get_channel_layer
from redis.exceptions import ConnectionError as RedisConnectionError
from accounts.models import Role, User
from rest_framework.authtoken.models import Token
from channels_redis.core import RedisChannelLayer


class ReleaseRealtimeTests(TransactionTestCase):
    def setUp(self):
        self.token = Token.objects.create(user=User.objects.create_user(username="synthetic-release-ws", role=Role.DISPATCHER)).key

    async def test_channel_transport_outage_closes_with_retryable_code(self):
        client = WebsocketCommunicator(application, "/ws/dispatcher/", headers=[(b"origin", b"http://localhost:8000")])
        real_receive = RedisChannelLayer.receive
        armed = False

        async def interrupted(layer, channel):
            event = await real_receive(layer, channel)
            if armed:
                raise RedisConnectionError("synthetic-private")
            return event

        # Channels binds receive at connection startup; inject the transport then.
        with patch.object(RedisChannelLayer, "receive", new=interrupted):
            try:
                self.assertTrue((await client.connect())[0])
                await client.send_json_to({"type": "authenticate", "token": self.token})
                self.assertEqual((await client.receive_json_from())["type"], "ready")
                armed = True
                await get_channel_layer().group_send("dispatchers", {"type": "domain.event", "payload": {"type": "test.event"}})
                self.assertEqual((await client.receive_output(timeout=3))["code"], 1013)
            finally:
                await client.disconnect()

    async def test_auth_database_outage_closes_with_retryable_code(self):
        client = WebsocketCommunicator(application, "/ws/dispatcher/", headers=[(b"origin", b"http://localhost:8000")])
        try:
            self.assertTrue((await client.connect())[0])
            with patch("realtime.consumers.identity", new_callable=AsyncMock, side_effect=OperationalError("synthetic-private")):
                await client.send_json_to({"type": "authenticate", "token": "x" * 40})
                self.assertEqual((await client.receive_output(timeout=2))["code"], 1013)
        finally:
            await client.disconnect()
