"""Synchronous spatial candidate search and dispatcher dismissals; never auto-merge."""

from datetime import timedelta

from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.db import transaction
from django.db.models import Exists, FloatField, OuterRef, Q, Subquery
from django.db.models.functions import Cast, Coalesce
from rest_framework.exceptions import NotFound, ValidationError

from accounts.models import Role

from .models import DuplicateDismissal, Incident, IncidentReport, IncidentStatus
from .services import Conflict, locked_object, require_role


ELIGIBLE_REPORT_STATUSES = (IncidentReport.ReviewStatus.PENDING, IncidentReport.ReviewStatus.ACCEPTED)
CLOSED_INCIDENT_STATUSES = (IncidentStatus.RESOLVED, IncidentStatus.CANCELLED)


def candidate_queries(report):
    """Return lazy SQL querysets. Only bounded results are materialized by the caller."""
    if report.is_draft or report.incident_id or report.review_status not in ELIGIBLE_REPORT_STATUSES:
        return IncidentReport.objects.none(), Incident.objects.none()
    effective_at = report.occurred_at or report.submitted_at or report.created_at
    window = timedelta(minutes=settings.CLUSTER_TIME_WINDOW_MINUTES)
    start, end = effective_at - window, effective_at + window
    radius = D(m=settings.CLUSTER_RADIUS_METERS)
    dismissed_incidents = DuplicateDismissal.objects.filter(
        report_id=report.pk, incident__isnull=False,
    ).values("incident_id")
    dismissed_pair = DuplicateDismissal.objects.filter(
        Q(report_id=report.pk, candidate_report_id=OuterRef("pk"))
        | Q(report_id=OuterRef("pk"), candidate_report_id=report.pk)
    )
    reports = (
        IncidentReport.objects.alias(effective_at=Coalesce("occurred_at", "submitted_at", "created_at"))
        .filter(
            is_draft=False, category_id=report.category_id, review_status__in=ELIGIBLE_REPORT_STATUSES,
            effective_at__range=(start, end), location__dwithin=(report.location, radius),
        )
        .exclude(pk=report.pk)
        .exclude(incident__status__in=CLOSED_INCIDENT_STATUSES)
        .exclude(incident_id__in=Subquery(dismissed_incidents))
        .filter(~Exists(dismissed_pair))
        .annotate(distance_meters=Cast(Distance("location", report.location, spheroid=True), FloatField()))
        .order_by("distance_meters", "pk")
    )
    # A report can witness an incident even if the incident's original point/time differs.
    witness = reports.filter(incident_id=OuterRef("pk"))
    direct_ids = (
        Incident.objects.filter(
            category_id=report.category_id, location__dwithin=(report.location, radius),
            created_at__range=(start, end),
        )
        .exclude(status__in=CLOSED_INCIDENT_STATUSES)
        .order_by().values("pk")
    )
    linked_ids = reports.filter(incident__isnull=False).order_by().values("incident_id")
    incidents = (
        Incident.objects.filter(pk__in=Subquery(direct_ids.union(linked_ids)))
        .exclude(pk__in=Subquery(dismissed_incidents))
        .annotate(
            matched_report_id=Subquery(witness.values("pk")[:1]),
            distance_meters=Coalesce(
                Subquery(witness.values("distance_meters")[:1]),
                Cast(Distance("location", report.location, spheroid=True), FloatField()),
                output_field=FloatField(),
            ),
        )
        .order_by("distance_meters", "pk")
    )
    return reports, incidents


def submission_hint(report):
    """Do not disclose other citizens' report IDs, positions or incident details."""
    reports, incidents = candidate_queries(report)
    has_candidates = reports.exists() or incidents.exists()
    return {"has_candidates": has_candidates, "dispatcher_confirmation_required": has_candidates}


def find_potential_duplicates(*, actor, report_id):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    try:
        report = IncidentReport.objects.get(pk=report_id, is_draft=False)
    except IncidentReport.DoesNotExist as exc:
        raise NotFound("Report not found.") from exc
    report_query, incident_query = candidate_queries(report)
    limit = settings.CLUSTER_MAX_CANDIDATES
    reports = list(report_query[:limit + 1])
    incidents = list(incident_query[:limit + 1])
    return {
        "source_report_id": report.pk,
        "radius_meters": settings.CLUSTER_RADIUS_METERS,
        "time_window_minutes": settings.CLUSTER_TIME_WINDOW_MINUTES,
        "effective_at": report.occurred_at or report.submitted_at or report.created_at,
        "reports": reports[:limit], "incidents": incidents[:limit],
        "more_reports": len(reports) > limit, "more_incidents": len(incidents) > limit,
    }


@transaction.atomic
def dismiss_potential_duplicate(*, actor, report_id, candidate_report_id=None, incident_id=None, reason=""):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    if (candidate_report_id is None) == (incident_id is None):
        raise ValidationError("Specify exactly one candidate_report_id or incident_id.")
    if candidate_report_id is not None:
        if candidate_report_id == report_id:
            raise ValidationError("A report cannot be a duplicate of itself.")
        ids = sorted([report_id, candidate_report_id])
        locked = list(IncidentReport.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
        if len(locked) != 2:
            raise NotFound("Report not found.")
        report = next(item for item in locked if item.pk == report_id)
        lookup = {"report_id": ids[0], "candidate_report_id": ids[1]}
    else:
        # Same lock order as link_reports: incident, then report.
        locked_object(Incident, incident_id)
        report = locked_object(IncidentReport, report_id)
        lookup = {"report_id": report_id, "incident_id": incident_id}
    existing = DuplicateDismissal.objects.filter(**lookup).first()
    if existing:
        return existing
    reports, incidents = candidate_queries(report)
    candidates, target_id = (reports, candidate_report_id) if candidate_report_id is not None else (incidents, incident_id)
    if not candidates.filter(pk=target_id).exists():
        raise Conflict("This target is no longer a potential duplicate. Refresh the suggestions.")
    return DuplicateDismissal.objects.create(**lookup, dismissed_by=actor, reason=reason)
