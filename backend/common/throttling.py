"""Per-user guards; unavailable cache fails closed without leaking details."""
import logging
from django.conf import settings
from redis.exceptions import RedisError
from rest_framework.exceptions import APIException
from rest_framework.throttling import ScopedRateThrottle

logger = logging.getLogger(__name__)


class RateGuardUnavailable(APIException):
    status_code = 503
    default_detail = "Service temporarily unavailable. Retry later."


class SafeScopedRateThrottle(ScopedRateThrottle):
    def get_rate(self):
        return settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"][self.scope]

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except (ConnectionError, TimeoutError, OSError, RedisError):
            logger.warning("Rate guard cache unavailable")
            raise RateGuardUnavailable from None


class NotificationRateMixin:
    throttle_classes = [SafeScopedRateThrottle]
    throttle_scope = "notification"
