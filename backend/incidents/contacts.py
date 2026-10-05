"""Contact disclosure is explicit, scoped and excluded from broadcast payloads."""
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import NotFound, ValidationError
from accounts.models import Role
from dispatch.models import ACTIVE_ASSIGNMENT_STATUSES
from .models import ContactAccess, Incident, IncidentReport
from .services import require_role


def masked_phone(value):
    return ("•" * max(0, len(value) - 3) + value[-3:]) if value else ""


def reports_for(actor):
    require_role(actor, (Role.CITIZEN, Role.DISPATCHER, Role.ADMIN, Role.RESCUE_TEAM))
    query = IncidentReport.objects.select_related("incident")
    if actor.role == Role.CITIZEN:
        return query.filter(reporter=actor)
    query = query.filter(is_draft=False)
    if actor.role == Role.RESCUE_TEAM:
        if not actor.response_team_id:
            return query.none()
        return query.filter(incident__assignments__team_id=actor.response_team_id,
                            incident__assignments__status__in=ACTIVE_ASSIGNMENT_STATUSES,
                            incident__status__in=["verified", "dispatched", "in_progress"]).distinct()
    return query


def contact_for(actor, report, purpose=""):
    # Recheck even when called outside a view; never trust a client-selected report.
    report = get_object_or_404(reports_for(actor), pk=report.pk)
    if len(purpose) > 200:
        raise ValidationError({"purpose": "Maximum 200 characters."})
    open_report = report.review_status != "rejected" and (
        not report.incident_id or report.incident.status not in ("resolved", "cancelled"))
    admin_access = actor.role == Role.ADMIN and bool(purpose.strip())
    if admin_access:
        ContactAccess.objects.create(report=report, actor=actor, purpose=purpose.strip())
    full = actor.pk == report.reporter_id or admin_access or (
        report.allow_contact and open_report and actor.role in (Role.DISPATCHER, Role.RESCUE_TEAM))
    return {"report_id": report.pk, "reporter_name": report.reporter_name,
            "reporter_phone": report.reporter_phone if full else masked_phone(report.reporter_phone),
            "phone_masked": not full, "allow_contact": report.allow_contact,
            "can_call": bool(full and report.allow_contact and report.reporter_phone and open_report),
            "latitude": report.location.y, "longitude": report.location.x,
            "location_accuracy": report.location_accuracy, "reported_at": report.submitted_at or report.created_at}


def incident_contacts(actor, incident_id, purpose=""):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    incident = get_object_or_404(Incident, pk=incident_id)
    return [contact_for(actor, report, purpose) for report in incident.reports.order_by("created_at", "pk")]
