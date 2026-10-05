from datetime import timedelta

from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError

from accounts.models import Role
from incidents.services import Conflict, locked_object, require_role
from realtime.events import publish_after_commit
from .models import ResponseTeam
from .serializers import GPSUpdateSerializer, TeamLocationSerializer


@transaction.atomic
def update_location(*, actor, data):
    require_role(actor, (Role.RESCUE_TEAM,))
    if not actor.response_team_id:
        raise PermissionDenied("Your account has no response team.")
    serializer = GPSUpdateSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    values = serializer.validated_data
    team = locked_object(ResponseTeam, actor.response_team_id)
    now = timezone.now()
    timestamp = values["timestamp"]
    if timestamp < now - timedelta(seconds=settings.GPS_MAX_AGE_SECONDS):
        raise ValidationError({"timestamp": "GPS sample is too old."})
    if timestamp > now + timedelta(seconds=settings.GPS_FUTURE_TOLERANCE_SECONDS):
        raise ValidationError({"timestamp": "GPS sample is in the future."})
    if team.location_updated_at and timestamp <= team.location_updated_at:
        raise Conflict("A newer or equal GPS sample is already stored.")
    if team.location_received_at:
        elapsed = (now - team.location_received_at).total_seconds()
        if elapsed < settings.GPS_MIN_INTERVAL_SECONDS:
            raise Throttled(wait=settings.GPS_MIN_INTERVAL_SECONDS - elapsed)
    team.last_location = Point(values["longitude"], values["latitude"], srid=4326)
    team.location_accuracy = values["accuracy"]
    team.location_updated_at = timestamp
    team.location_received_at = now
    team.save(update_fields=["last_location", "location_accuracy", "location_updated_at", "location_received_at", "updated_at"])
    publish_after_commit(["dispatchers"], "team.location_updated", dict(TeamLocationSerializer(team).data))
    return team
