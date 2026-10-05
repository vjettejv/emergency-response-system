"""Truthful lifecycle timestamps and explicitly defined duration metrics."""
from django.db.models import Min, Max
from django.db.models.functions import Coalesce


def source_times(incident):
    reports = incident.reports.aggregate(report_created_at=Min(Coalesce("submitted_at", "created_at")))
    verified = incident.reports.filter(review_status="accepted").aggregate(value=Min("reviewed_at"))["value"]
    if verified is None:
        verified = incident.status_history.filter(to_status="verified").aggregate(value=Min("changed_at"))["value"]
    return {**reports, "verified_at": verified}


def assignment_timeline(assignment):
    return {**source_times(assignment.incident), **{key: getattr(assignment, key) for key in (
        "dispatched_at", "accepted_at", "en_route_at", "arrived_at", "responding_at", "completed_at")}}


def seconds(start, end):
    return max(0, (end - start).total_seconds()) if start and end and end >= start else None


def incident_timeline(incident):
    timeline = source_times(incident)
    timeline.update(incident.assignments.aggregate(**{key: Min(key) for key in (
        "dispatched_at", "accepted_at", "en_route_at", "arrived_at", "responding_at")}))
    # Completion is per assignment; do not mistake one team's completion for incident resolution.
    resolved = incident.status_history.filter(to_status="resolved").aggregate(value=Min("changed_at"))["value"]
    from .models import ACTIVE_ASSIGNMENT_STATUSES
    timeline["completed_at"] = None if incident.assignments.filter(status__in=ACTIVE_ASSIGNMENT_STATUSES).exists() else incident.assignments.aggregate(value=Max("completed_at"))["value"]
    return {"incident_id": incident.pk, "milestones": timeline, "resolved_at": resolved,
            "time_to_accept_seconds": seconds(timeline["report_created_at"], timeline["accepted_at"]),
            "time_to_resolve_seconds": seconds(timeline["report_created_at"], resolved),
            "assignments": [{"assignment_id": a.pk, "team_id": a.team_id, "status": a.status,
                **{key: getattr(a, key) for key in ("dispatched_at", "accepted_at", "en_route_at", "arrived_at", "responding_at", "completed_at")}}
                for a in incident.assignments.order_by("pk")]}


def response_metrics():
    """Aggregate per-incident durations in SQL; missing milestones are excluded."""
    from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, OuterRef, Subquery
    from incidents.models import Incident, IncidentReport, IncidentStatusHistory
    from .models import Assignment
    query = Incident.objects.annotate(
        received=Subquery(IncidentReport.objects.filter(incident_id=OuterRef("pk"), is_draft=False).annotate(received_time=Coalesce("submitted_at", "created_at")).order_by("received_time").values("received_time")[:1]),
        accepted=Subquery(Assignment.objects.filter(incident_id=OuterRef("pk"), accepted_at__isnull=False).order_by("accepted_at").values("accepted_at")[:1]),
        resolved=Subquery(IncidentStatusHistory.objects.filter(incident_id=OuterRef("pk"), to_status="resolved").order_by("changed_at").values("changed_at")[:1]),
    )
    result = {}
    for metric, milestone in (("mtta", "accepted"), ("mttr", "resolved")):
        measured = query.filter(**{f"{milestone}__gte": F("received")}).annotate(
            elapsed=ExpressionWrapper(F(milestone) - F("received"), output_field=DurationField()))
        aggregate = measured.aggregate(mean=Avg("elapsed"), sample_count=Count("pk"))
        result[f"{metric}_seconds"] = aggregate["mean"].total_seconds() if aggregate["mean"] is not None else None
        result[f"{metric}_sample_count"] = aggregate["sample_count"]
    return result
