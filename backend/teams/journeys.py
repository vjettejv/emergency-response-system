"""Durable, bounded GPS history. No reconstruction from the latest-location snapshot."""
import math
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from accounts.models import Role
from dispatch.models import ACTIVE_ASSIGNMENT_STATUSES, Assignment
from incidents.services import locked_object, require_role
from .models import GPSJourney, GPSPoint, ResponseTeam

HISTORY_ROLES = (Role.DISPATCHER, Role.ADMIN, Role.RESCUE_TEAM)


def record_sample(*, team, timestamp, received_at):
    """Called inside update_location's transaction, with the team locked."""
    assignment_id = Assignment.objects.filter(team=team, status__in=ACTIVE_ASSIGNMENT_STATUSES,
                                               dispatched_at__lte=timestamp).values_list("pk", flat=True).first()
    journey = GPSJourney.objects.filter(team=team, ended_at__isnull=True).first()
    if journey and (journey.assignment_id != assignment_id or
                    (timestamp - journey.last_recorded_at).total_seconds() > settings.GPS_JOURNEY_GAP_SECONDS):
        journey.ended_at = journey.last_recorded_at
        journey.save(update_fields=["ended_at"])
        journey = None
    if journey is None:
        journey = GPSJourney.objects.create(team=team, assignment_id=assignment_id, started_at=timestamp, last_recorded_at=timestamp)
    GPSPoint.objects.create(journey=journey, location=team.last_location, recorded_at=timestamp,
                            received_at=received_at, accuracy=team.location_accuracy)
    if journey.last_recorded_at != timestamp:
        journey.last_recorded_at = timestamp
        journey.save(update_fields=["last_recorded_at"])


@transaction.atomic
def stop_journey(*, actor):
    require_role(actor, (Role.RESCUE_TEAM,))
    if not actor.response_team_id:
        raise NotFound("Chưa được gán đội ứng cứu.")
    locked_object(ResponseTeam, actor.response_team_id)
    journey = GPSJourney.objects.filter(team_id=actor.response_team_id, ended_at__isnull=True).first()
    if journey:
        journey.ended_at = journey.last_recorded_at
        journey.save(update_fields=["ended_at"])


def visible_journeys(actor):
    require_role(actor, HISTORY_ROLES)
    query = GPSJourney.objects.select_related("team", "assignment__incident")
    if actor.role == Role.RESCUE_TEAM:
        query = query.filter(team_id=actor.response_team_id) if actor.response_team_id else query.none()
    return query


def time_range(values):
    end = values.get("until") or timezone.now()
    start = values.get("since") or end - timedelta(days=1)
    if start >= end or end - start > timedelta(days=settings.GPS_HISTORY_MAX_DAYS):
        raise ValidationError("Chọn khoảng thời gian hợp lệ, tối đa %s ngày." % settings.GPS_HISTORY_MAX_DAYS)
    return start, end


def journey_list(*, actor, values):
    start, end = time_range(values)
    query = visible_journeys(actor).filter(started_at__lte=end, last_recorded_at__gte=start)
    if values.get("team"):
        query = query.filter(team_id=values["team"])
    return query


def summary(journey):
    # A radio/network gap is not displayed as live tracking indefinitely.
    completed = bool(journey.assignment_id and journey.assignment.status not in ACTIVE_ASSIGNMENT_STATUSES)
    active = not completed and journey.ended_at is None and (timezone.now() - journey.last_recorded_at).total_seconds() <= settings.GPS_JOURNEY_GAP_SECONDS
    return {"id": journey.pk, "team": journey.team_id, "team_name": journey.team.name,
            "assignment": journey.assignment_id,
            "incident_title": journey.assignment.incident.title if journey.assignment_id else None,
            "started_at": journey.started_at, "last_recorded_at": journey.last_recorded_at,
            "ended_at": journey.ended_at, "tracking_state": "recording" if active else "ended" if journey.ended_at or completed else "signal_lost"}


def distance_meters(a, b):
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    delta_lat, delta_lon = lat2 - lat1, math.radians(b[0] - a[0])
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(value)))


def journey_detail(*, actor, journey_id, values):
    journey = visible_journeys(actor).filter(pk=journey_id).first()
    if journey is None:
        raise NotFound("Không tìm thấy hành trình.")
    start, end = time_range(values)
    # Bounded, indexed query over one authorized journey, never a fleet-wide Python scan.
    points = list(journey.points.filter(recorded_at__gte=start, recorded_at__lte=end)[:settings.GPS_HISTORY_MAX_POINTS + 1])
    if len(points) > settings.GPS_HISTORY_MAX_POINTS:
        raise ValidationError("Có quá nhiều điểm. Vui lòng thu hẹp khoảng thời gian.")
    segments, current, previous, distance = [], [], None, 0
    for point in points:
        coordinate = [point.location.x, point.location.y]
        if previous:
            elapsed = (point.recorded_at - previous.recorded_at).total_seconds()
            length = distance_meters([previous.location.x, previous.location.y], coordinate)
            if elapsed <= 0 or elapsed > settings.GPS_JOURNEY_GAP_SECONDS or length / elapsed > settings.GPS_HISTORY_MAX_SPEED_MPS:
                if current:
                    segments.append(current)
                current = []
            else:
                distance += length
        current.append(coordinate)
        previous = point
    if current:
        segments.append(current)
    return {**summary(journey), "since": start, "until": end, "distance_meters": round(distance, 1),
            "points": [{"latitude": p.location.y, "longitude": p.location.x, "timestamp": p.recorded_at, "accuracy": p.accuracy} for p in points],
            "segments": segments, "gap_seconds": settings.GPS_JOURNEY_GAP_SECONDS}
