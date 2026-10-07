import asyncio
import json
import logging
import time
from contextlib import suppress

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.db import DatabaseError
from rest_framework.authtoken.models import Token
from redis.exceptions import RedisError

from accounts.models import Role

logger = logging.getLogger(__name__)


@database_sync_to_async
def identity(token_key):
    token = Token.objects.select_related("user").filter(key=token_key, user__is_active=True).first()
    if not token:
        return None
    return (token.user_id, token.user.role, token.user.response_team_id)


class EventConsumer(AsyncJsonWebsocketConsumer):
    """First-frame token authentication. Never accept client-selected groups."""
    audience = None

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except RedisError:
            logger.warning("WebSocket channel unavailable")
            await self.close(code=1013)
        finally:
            # Also clean up when the channel layer fails before disconnect arrives.
            await self.cleanup()

    async def connect(self):
        self.bound_identity = None
        self.token_key = None
        self.group = None
        self.closed = False
        self.message_window = time.monotonic()
        self.message_count = 0
        await self.accept()
        self.watchdog = asyncio.create_task(self.monitor())

    async def monitor(self):
        await asyncio.sleep(settings.WS_AUTH_TIMEOUT_SECONDS)
        if not self.bound_identity:
            await self.close(code=4401)
            return
        while not self.closed:
            await asyncio.sleep(settings.WS_HEARTBEAT_SECONDS)
            if not await self.authorized():
                return
            try:
                await self.channel_layer.group_add(self.group, self.channel_name)
            except Exception:
                logger.warning("WebSocket channel unavailable")
                await self.close(code=1013)
                return
            await self.send_json({"type": "heartbeat"})

    async def authorized(self):
        if self.closed:
            return False
        current = await self.current_identity(self.token_key) if self.bound_identity else None
        if self.closed:
            return False
        if not self.bound_identity or current != self.bound_identity:
            await self.close(code=4403)
            return False
        return True

    async def current_identity(self, token):
        try:
            return await identity(token)
        except DatabaseError:
            logger.warning("WebSocket authentication database unavailable")
            await self.close(code=1013)
            return None

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        if self.closed:
            return
        now = time.monotonic()
        if now - self.message_window >= 1:
            self.message_window, self.message_count = now, 0
        self.message_count += 1
        if self.message_count > 10:
            await self.close(code=4429)
            return
        if text_data is None or len(text_data.encode("utf-8")) > 2048:
            await self.close(code=4400)
            return
        try:
            message = json.loads(text_data)
        except (ValueError, TypeError):
            await self.close(code=4400)
            return
        if not isinstance(message, dict):
            await self.close(code=4400)
            return
        if not self.bound_identity:
            if set(message) != {"type", "token"} or message["type"] != "authenticate":
                await self.close(code=4401)
                return
            token = message["token"]
            if not isinstance(token, str) or len(token) != 40:
                await self.close(code=4401)
                return
            user = await self.current_identity(token)
            if self.closed:
                return
            if not user:
                await self.close(code=4401)
                return
            if self.audience == "notification" and user[1] in Role.values:
                self.group = f"user_{user[0]}"
            elif self.audience == "dispatcher" and user[1] in (Role.DISPATCHER, Role.ADMIN):
                self.group = "dispatchers"
            elif self.audience == "rescue" and user[1] == Role.RESCUE_TEAM and user[2]:
                self.group = f"team.{user[2]}"
            else:
                await self.close(code=4403)
                return
            self.bound_identity, self.token_key = user, token
            try:
                await self.channel_layer.group_add(self.group, self.channel_name)
            except Exception:
                logger.warning("WebSocket channel unavailable")
                await self.close(code=1013)
                return
            await self.send_json({"type": "ready", "resync_required": True})
            return
        if await self.authorized():
            if message == {"type": "ping"}:
                await self.send_json({"type": "pong"})
            else:
                await self.send_json({"type": "error", "code": "unsupported_message"})

    async def domain_event(self, event):
        if await self.authorized():
            await self.send_json(event["payload"])

    async def close(self, code=None, reason=None):
        self.closed = True
        await super().close(code=code, reason=reason)

    async def disconnect(self, close_code):
        await self.cleanup()

    async def cleanup(self):
        self.closed = True
        watchdog = getattr(self, "watchdog", None)
        if watchdog:
            watchdog.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await watchdog
            self.watchdog = None
        if getattr(self, "group", None):
            try:
                await self.channel_layer.group_discard(self.group, self.channel_name)
            except Exception:
                logger.warning("WebSocket group cleanup unavailable")
            self.group = None


class DispatcherConsumer(EventConsumer):
    audience = "dispatcher"


class RescueConsumer(EventConsumer):
    audience = "rescue"


class NotificationConsumer(EventConsumer):
    audience = "notification"
