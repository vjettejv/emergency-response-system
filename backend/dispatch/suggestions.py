"""PostGIS team search. Routing/ETA is unavailable until a real data source exists."""

from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.db.models import Exists, FloatField, IntegerField, OuterRef, Value
from django.db.models.functions import Cast
from rest_framework.exceptions import NotFound

from accounts.models import Role
from incidents.models import Incident
from incidents.services import require_role
from teams.models import ResponseTeam

from .models import ACTIVE_ASSIGNMENT_STATUSES, Assignment
from .services import ensure_dispatchable


def eligible_team_query(incident):
    occupied = Assignment.objects.filter(team_id=OuterRef("pk"), status__in=ACTIVE_ASSIGNMENT_STATUSES)
    return (
        ResponseTeam.objects.filter(
            categories=incident.category_id, status=ResponseTeam.Status.AVAILABLE,
            last_location__isnull=False,
            last_location__dwithin=(incident.location, D(m=settings.DISPATCH_RADIUS_METERS)),
        )
        .filter(~Exists(occupied))
        .annotate(distance_meters=Cast(Distance("last_location", incident.location, spheroid=True), FloatField()))
        .annotate(eta_seconds=Value(None, output_field=IntegerField()))
        .order_by("distance_meters", "pk")
    )


def suggest_teams(*, actor, incident_id):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    try:
        incident = Incident.objects.get(pk=incident_id)
    except Incident.DoesNotExist as exc:
        raise NotFound("Incident not found.") from exc
    ensure_dispatchable(incident)
    limit = settings.DISPATCH_MAX_SUGGESTIONS
    teams = list(eligible_team_query(incident)[:limit + 1])
    return {
        "incident_id": incident.pk, "radius_meters": settings.DISPATCH_RADIUS_METERS,
        "ranking": "geographic_distance", "routing_available": False,
        "teams": teams[:limit], "more_teams": len(teams) > limit,
    }
