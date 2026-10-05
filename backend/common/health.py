from django.conf import settings
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from redis import Redis, RedisError


@require_GET
def health(request):
    database = redis = "unavailable"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() == (1,):
                database = "ok"
    except DatabaseError:
        pass
    try:
        with Redis.from_url(settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"][0]["address"],
                            socket_connect_timeout=1, socket_timeout=1) as client:
            if client.ping():
                redis = "ok"
    except (RedisError, OSError, ValueError):
        pass
    ready = database == redis == "ok"
    response = JsonResponse({"status": "ok" if ready else "unavailable", "database": database, "redis": redis},
                            status=200 if ready else 503)
    response["Cache-Control"] = "no-store"
    return response
