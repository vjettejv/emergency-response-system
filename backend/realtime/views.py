from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def frontend(request, asset="index.html"):
    # Public shell only. All operational data needs authenticated REST/WS access.
    if asset == "config.json":
        return JsonResponse({"apiBase": "/api/v1/", "dispatcherSocket": "/ws/dispatcher/", "rescueSocket": "/ws/rescue/", "mediaMaxBytes": settings.MEDIA_MAX_BYTES, "gpsIntervalMs": max(10, settings.GPS_MIN_INTERVAL_SECONDS) * 1000, "tileUrl": settings.LEAFLET_TILE_URL})
    if asset not in ("index.html", "app.js", "client.js", "map.js", "style.css"):
        raise Http404
    content_type = "text/javascript" if asset.endswith(".js") else "text/css" if asset.endswith(".css") else "text/html"
    response = FileResponse((Path(__file__).parent / "frontend" / asset).open("rb"), content_type=content_type)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
