from django.db import transaction, IntegrityError
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from accounts.models import Role
from realtime.events import incident_changed, publish_after_commit, report_changed
from notifications import services as notifications

from .models import Incident, IncidentReport, IncidentReportLinkHistory, IncidentStatus, IncidentStatusHistory


class Conflict(APIException):
    status_code = 409
    default_detail = "The resource has changed or conflicts with this operation."
    default_code = "conflict"


@transaction.atomic
def save_report_draft(*, actor, request_id, values):
    """A private parent for camera PUTs. Repeated request UUID updates the same draft."""
    require_role(actor, (Role.CITIZEN,))
    query = IncidentReport.objects.select_for_update()
    report = query.filter(draft_request_id=request_id).first()
    if report is None:
        try:
            with transaction.atomic():
                report = IncidentReport.objects.create(reporter=actor, is_draft=True, draft_request_id=request_id, **values)
        except IntegrityError:
            report = query.get(draft_request_id=request_id)
    if report.reporter_id != actor.pk:
        raise NotFound("Draft not found.")
    if not report.is_draft:
        return report  # Finalization response may have been lost; never duplicate submission.
    for key, value in {"occurred_at": None, "media_metadata": [], **values}.items():
        setattr(report, key, value)
    report.save()
    return report


@transaction.atomic
def finalize_report(*, actor, report_id):
    require_role(actor, (Role.CITIZEN,))
    report = locked_object(IncidentReport, report_id)
    if report.reporter_id != actor.pk:
        raise NotFound("Draft not found.")
    if not report.is_draft:
        return report
    if not report.reporter_name.strip() or not report.reporter_phone:
        raise Conflict("Add a reporter name and phone before sending the draft.")
    if not report.category.is_active:
        raise Conflict("Choose an active category before sending the draft.")
    if report.media_assets.select_for_update().exclude(status__in=["ready", "deleting", "deleted", "failed"]).exists():
        raise Conflict("Confirm or remove unfinished uploads before submitting the report.")
    report.is_draft = False
    report.submitted_at = timezone.now()
    report.save(update_fields=["is_draft", "submitted_at", "updated_at"])
    from .clustering import submission_hint
    report.potential_duplicate_hint = submission_hint(report)
    report_changed(report)
    notifications.report_submitted(report)
    return report


def require_role(actor, roles):
    if not actor or not actor.is_authenticated or not actor.is_active or actor.role not in roles:
        raise PermissionDenied("Your role cannot perform this operation.")


def locked_object(model, pk):
    try:
        return model.objects.select_for_update().get(pk=pk)
    except model.DoesNotExist as exc:
        raise NotFound(f"{model.__name__} not found.") from exc


@transaction.atomic
def submit_report(*, actor, category, description, location, address="", occurred_at=None, media_metadata=None,
                  reporter_name="", reporter_phone="", allow_contact=True, location_accuracy=None, gps_location=None):
    """Accept serializer-validated input; keep ownership and review fields server-owned."""
    require_role(actor, (Role.CITIZEN,))
    if not category.is_active:
        raise ValidationError({"category": "This category is inactive."})
    report = IncidentReport.objects.create(
        reporter=actor, category=category, description=description, location=location,
        address=address, occurred_at=occurred_at, media_metadata=media_metadata or [],
        reporter_name=reporter_name, reporter_phone=reporter_phone,
        allow_contact=allow_contact, location_accuracy=location_accuracy, gps_location=gps_location,
    )
    # Local import avoids a cycle with the clustering service's permission helpers.
    from .clustering import submission_hint

    report.potential_duplicate_hint = submission_hint(report)
    report_changed(report)
    notifications.report_submitted(report)
    return report


@transaction.atomic
def review_report(*, actor, report_id, review_status, note=""):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    allowed = (IncidentReport.ReviewStatus.ACCEPTED, IncidentReport.ReviewStatus.REJECTED)
    if review_status not in allowed:
        raise ValidationError({"review_status": "Choose accepted or rejected."})
    report = locked_object(IncidentReport, report_id)
    if report.is_draft:
        raise NotFound("Report not found.")
    if report.review_status == review_status:
        return report  # Retrying a decision must not overwrite its original audit trail.
    if report.review_status != IncidentReport.ReviewStatus.PENDING or report.incident_id:
        raise Conflict("A reviewed or linked report cannot be reclassified in this phase.")
    report.review_status = review_status
    report.reviewed_by = actor
    report.reviewed_at = timezone.now()
    report.review_note = note
    report.save(update_fields=["review_status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])
    report_changed(report)
    notifications.report_reviewed(report)
    return report


@transaction.atomic
def create_incident_from_report(*, actor, report_id, title):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    report = locked_object(IncidentReport, report_id)
    if report.is_draft:
        raise NotFound("Report not found.")
    if report.review_status != IncidentReport.ReviewStatus.ACCEPTED:
        raise Conflict("Accept the report before creating an incident.")
    if report.incident_id:
        raise Conflict("This report already belongs to an incident.")
    incident = Incident.objects.create(
        title=title, description=report.description, category_id=report.category_id,
        location=report.location.clone(), address=report.address,
        status=IncidentStatus.VERIFIED, created_by=actor,
    )
    report.incident = incident
    report.save(update_fields=["incident", "updated_at"])
    IncidentStatusHistory.objects.create(
        incident=incident, from_status="", to_status=incident.status, changed_by=actor,
        note=f"Created from accepted report {report.pk}.",
    )
    IncidentReportLinkHistory.objects.create(
        incident=incident, report=report, actor=actor, operation=IncidentReportLinkHistory.Operation.CREATE,
    )
    incident_changed(incident)
    notifications.report_linked(report, incident.pk)
    return incident


@transaction.atomic
def link_reports(*, actor, incident_id, report_ids, note=""):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    incident = locked_object(Incident, incident_id)
    if incident.status in (IncidentStatus.RESOLVED, IncidentStatus.CANCELLED):
        raise Conflict("Cannot add reports to a closed incident.")
    # Always lock the incident first, then reports in primary-key order.
    reports = list(IncidentReport.objects.select_for_update().filter(pk__in=report_ids).order_by("pk"))
    if len(reports) != len(set(report_ids)):
        raise NotFound("One or more reports do not exist.")
    for report in reports:
        if report.is_draft:
            raise NotFound("Report not found.")
        if report.review_status != IncidentReport.ReviewStatus.ACCEPTED:
            raise Conflict(f"Report {report.pk} has not been accepted.")
        if report.category_id != incident.category_id:
            raise Conflict(f"Report {report.pk} has a different category.")
        if report.incident_id not in (None, incident.pk):
            raise Conflict(f"Report {report.pk} already belongs to another incident.")
    # Validate the entire batch before writing. Re-linking to the same incident is a no-op.
    IncidentReport.objects.filter(pk__in=report_ids, incident__isnull=True).update(
        incident=incident, updated_at=timezone.now(),
    )
    operation = (
        IncidentReportLinkHistory.Operation.MERGE if len(report_ids) > 1
        else IncidentReportLinkHistory.Operation.LINK
    )
    IncidentReportLinkHistory.objects.bulk_create([
        IncidentReportLinkHistory(incident=incident, report=report, actor=actor, operation=operation, note=note)
        for report in reports if report.incident_id is None
    ])
    linked_ids = [report.pk for report in reports if report.incident_id is None]
    if linked_ids:
        publish_after_commit(["dispatchers"], "reports.linked", {"incident_id": incident.pk, "report_ids": linked_ids})
        for report in reports:
            if report.pk in linked_ids:
                notifications.report_linked(report, incident.pk)
    return incident


# Dispatch enters DISPATCHED through its transactional service, not this manual API.
STATUS_TRANSITIONS = {
    IncidentStatus.NEW: {IncidentStatus.VERIFIED, IncidentStatus.CANCELLED},
    IncidentStatus.VERIFIED: {IncidentStatus.IN_PROGRESS, IncidentStatus.CANCELLED},
    IncidentStatus.DISPATCHED: {IncidentStatus.IN_PROGRESS, IncidentStatus.CANCELLED},
    IncidentStatus.IN_PROGRESS: {IncidentStatus.RESOLVED, IncidentStatus.CANCELLED},
    IncidentStatus.RESOLVED: set(),
    IncidentStatus.CANCELLED: set(),
}


@transaction.atomic
def change_incident_status(*, actor, incident_id, status, expected_status, note=""):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    incident = locked_object(Incident, incident_id)
    if incident.status != expected_status:
        raise Conflict("Incident status changed. Refresh before submitting again.")
    if status == incident.status:
        return incident
    if status not in STATUS_TRANSITIONS.get(incident.status, set()):
        raise Conflict(f"Cannot change incident from {incident.status} to {status}.")
    if status in (IncidentStatus.RESOLVED, IncidentStatus.CANCELLED):
        from dispatch.models import ACTIVE_ASSIGNMENT_STATUSES

        if incident.assignments.filter(status__in=ACTIVE_ASSIGNMENT_STATUSES).exists():
            raise Conflict("Finish or cancel active assignments before closing the incident.")
    previous = incident.status
    incident.status = status
    incident.save(update_fields=["status", "updated_at"])
    history = IncidentStatusHistory.objects.create(
        incident=incident, from_status=previous, to_status=status, changed_by=actor, note=note,
    )
    incident_changed(incident)
    notifications.incident_updated(incident, history.pk)
    return incident
