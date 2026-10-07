"""Opt-in Redis restart validation against a disposable named container only.

Set RELEASE_REDIS_URL=redis://ers-phase9-redis:6379/0. The harness announces
WAITING_FOR_RESTART; the operator restarts that container, never shared Redis.
"""
import asyncio
import os
import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.test import TransactionTestCase, override_settings
from rest_framework.authtoken.models import Token
from redis import Redis
from redis.exceptions import RedisError
from accounts.models import Role, User
from config.asgi import application


class RedisRestartRecoveryTests(TransactionTestCase):
    def setUp(self):
        url = os.environ.get("RELEASE_REDIS_URL", "")
        if url != "redis://ers-phase9-redis:6379/0":
            raise RuntimeError("Recovery requires the named disposable Redis test container")
        self.redis = Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
        self.addCleanup(self.redis.close)
        self.initial = self.redis.info("server")["run_id"]
        self.token = Token.objects.create(user=User.objects.create_user(username="synthetic-restart", role=Role.DISPATCHER)).key
        layers = {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {
            "hosts": [{"address": url, "socket_timeout": 10, "socket_connect_timeout": 1}],
            "prefix": "test.restart." + uuid4().hex,
        }}}
        context = override_settings(CHANNEL_LAYERS=layers, WS_AUTH_TIMEOUT_SECONDS=.05, WS_HEARTBEAT_SECONDS=.2)
        context.enable(); self.addCleanup(context.disable)

    async def test_restart_then_fresh_socket_recovers_and_resyncs(self):
        client = WebsocketCommunicator(application, "/ws/dispatcher/", headers=[(b"origin", b"http://localhost:8000")])
        try:
            self.assertTrue((await client.connect())[0])
            await client.send_json_to({"type": "authenticate", "token": self.token})
            self.assertEqual((await client.receive_json_from())["type"], "ready")
            print("RELEASE_RECOVERY WAITING_FOR_RESTART", flush=True)
            deadline = time.monotonic() + 50
            changed = False
            while time.monotonic() < deadline:
                try:
                    info = await asyncio.to_thread(self.redis.info, "server")
                    if info["run_id"] != self.initial:
                        changed = True; break
                except RedisError:
                    pass  # Expected only in this explicit outage test, bounded to 50 s.
                await asyncio.sleep(.1)
            self.assertTrue(changed, "Disposable Redis was not restarted")
        finally:
            await client.disconnect(timeout=5)
        fresh = WebsocketCommunicator(application, "/ws/dispatcher/", headers=[(b"origin", b"http://localhost:8000")])
        try:
            self.assertTrue((await fresh.connect())[0])
            await fresh.send_json_to({"type": "authenticate", "token": self.token})
            ready = await fresh.receive_json_from()
            self.assertTrue(ready["resync_required"])
            await fresh.send_json_to({"type": "ping"})
            self.assertEqual((await fresh.receive_json_from())["type"], "pong")
            print("RELEASE_RECOVERY RECONNECTED_AND_RESYNC_REQUIRED", flush=True)
        finally:
            await fresh.disconnect()
