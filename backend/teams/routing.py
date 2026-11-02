"""Opt-in OSRM Route API adapter; never return straight-line estimates on failure."""
import hashlib
import json
import math
from datetime import timedelta
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, Throttled, ValidationError

from accounts.models import Role
from dispatch.models import ACTIVE_ASSIGNMENT_STATUSES
from dispatch.services import assignments_for
from incidents.services import require_role


class RoutingUnavailable(APIException):
    status_code = 503
    default_detail = "Chưa thể tính tuyến đường. Vui lòng thử lại sau."


class NoRoute(APIException):
    status_code = 422
    default_detail = "Không tìm được tuyến đường giữa hai vị trí này."


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError
    return value


def _parse(data):
    if data.get("code") in ("NoRoute", "NoSegment"):
        raise NoRoute()
    if data.get("code") != "Ok":
        raise ValueError
    route = data["routes"][0]
    geometry = route["geometry"]
    coordinates = geometry["coordinates"]
    if geometry["type"] != "LineString" or not isinstance(coordinates, list) or not 2 <= len(coordinates) <= 20000:
        raise ValueError
    for coordinate in coordinates:
        if not isinstance(coordinate, list) or len(coordinate) != 2:
            raise ValueError
        lon, lat = coordinate
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in coordinate):
            raise ValueError
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError
    return {"geometry": {"type": "LineString", "coordinates": coordinates},
            "distance_meters": _number(route["distance"]), "duration_seconds": _number(route["duration"])}


def route_between(*, actor, origin, destination):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN, Role.RESCUE_TEAM))
    base = settings.ROUTING_BASE_URL
    if not base:
        raise RoutingUnavailable("Chức năng định tuyến chưa được cấu hình.")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RoutingUnavailable()
    # Endpoint is operator-configured, never taken from a request. No redirects, no raw URL/coordinate logging.
    coordinates = ";".join(f"{point['longitude']:.6f},{point['latitude']:.6f}" for point in (origin, destination))
    key = "routing:" + hashlib.sha256((base + coordinates).encode()).hexdigest()
    try:
        result = cache.get(key)
        if result is None:
            if not cache.add("routing:upstream_slot", True, timeout=1):
                raise Throttled(wait=1)
            request = Request(base + "/route/v1/driving/" + coordinates + "?overview=full&geometries=geojson&steps=false&alternatives=false",
                              headers={"Accept": "application/json", "User-Agent": settings.ROUTING_USER_AGENT})
            try:
                response = build_opener(NoRedirect()).open(request, timeout=settings.ROUTING_TIMEOUT_SECONDS)
            except HTTPError as error:
                if error.code == 400:
                    raw = error.read(10001)
                    if len(raw) <= 10000:
                        _parse(json.loads(raw))
                raise RoutingUnavailable() from None
            with response:
                raw = response.read(1000001)
            if len(raw) > 1000000:
                raise ValueError
            result = _parse(json.loads(raw))
            cache.set(key, result, timeout=settings.ROUTING_CACHE_SECONDS)
        now = timezone.now()
        return {**result, "calculated_at": now, "estimated_arrival_at": now + timedelta(seconds=result["duration_seconds"]),
                "traffic_aware": False, "provider": "OSRM", "origin": origin, "destination": destination,
                "refresh_seconds": settings.ROUTING_REFRESH_SECONDS}
    except (NoRoute, Throttled, RoutingUnavailable):
        raise
    except Exception:
        raise RoutingUnavailable() from None


def assignment_route(*, actor, assignment_id):
    assignment = assignments_for(actor).select_related("team", "incident").filter(pk=assignment_id).first()
    if assignment is None:
        raise NotFound("Không tìm thấy nhiệm vụ.")
    if assignment.status not in ACTIVE_ASSIGNMENT_STATUSES:
        raise ValidationError("Nhiệm vụ đã kết thúc. Hãy chọn vị trí thủ công để tìm tuyến khác.")
    team = assignment.team
    now = timezone.now()
    if (team.last_location is None or team.location_updated_at is None or
            (now - team.location_updated_at).total_seconds() > settings.GPS_MAX_AGE_SECONDS or
            team.location_updated_at > now + timedelta(seconds=settings.GPS_FUTURE_TOLERANCE_SECONDS)):
        raise ValidationError("Chưa có vị trí đội mới cập nhật để tính tuyến đường.")
    return route_between(actor=actor, origin={"latitude": team.last_location.y, "longitude": team.last_location.x},
                         destination={"latitude": assignment.incident.location.y, "longitude": assignment.incident.location.x})
