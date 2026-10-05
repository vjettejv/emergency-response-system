"""Dispatch transactions: lock Incident -> Team(s) in ID order -> Assignment."""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from accounts.models import Role
from incidents.models import Incident, IncidentStatus, IncidentStatusHistory
from incidents.services import Conflict, locked_object, require_role
from teams.models import ResponseTeam
from realtime.events import assignment_changed, incident_changed

from .models import ACTIVE_ASSIGNMENT_STATUSES, TERMINAL_ASSIGNMENT_STATUSES, Assignment, AssignmentHistory


DISPATCHABLE_INCIDENT_STATUSES = (IncidentStatus.VERIFIED, IncidentStatus.DISPATCHED, IncidentStatus.IN_PROGRESS)
RESCUE_TRANSITIONS = {
    Assignment.Status.PENDING: {Assignment.Status.ACCEPTED, Assignment.Status.REJECTED},
    Assignment.Status.ACCEPTED: {Assignment.Status.EN_ROUTE},
    Assignment.Status.EN_ROUTE: {Assignment.Status.ON_SCENE},
    Assignment.Status.ON_SCENE: {Assignment.Status.RESPONDING},
    Assignment.Status.RESPONDING: {Assignment.Status.COMPLETED},
}


def assignments_for(actor):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN, Role.RESCUE_TEAM))
    queryset = Assignment.objects.all()
    if actor.role == Role.RESCUE_TEAM:
        if not actor.response_team_id:
            return queryset.none()
        queryset = queryset.filter(team_id=actor.response_team_id)
    return queryset


def ensure_dispatchable(incident):
    if incident.status not in DISPATCHABLE_INCIDENT_STATUSES:
        raise Conflict("Incident must be verified and still open before dispatch.")


def _validate_team(team, incident):
    if team.status != ResponseTeam.Status.AVAILABLE:
        raise Conflict("The response team is not available.")
    if Assignment.objects.filter(team=team, status__in=ACTIVE_ASSIGNMENT_STATUSES).exists():
        raise Conflict("The team already has an active assignment.")
    if not team.categories.filter(pk=incident.category_id).exists():
        raise Conflict("The team does not support this incident category.")


def _incident_status(incident, target, actor, note):
    if incident.status == target:
        return
    previous = incident.status
    incident.status = target
    incident.save(update_fields=["status", "updated_at"])
    IncidentStatusHistory.objects.create(
        incident=incident, from_status=previous, to_status=target, changed_by=actor, note=note,
    )
    incident_changed(incident)


def _team_status(team, status):
    if team.status != status:
        team.status = status
        team.save(update_fields=["status", "updated_at"])


def _record(assignment, actor, operation, previous, note):
    AssignmentHistory.objects.create(
        assignment=assignment, actor=actor, operation=operation,
        from_status=previous, to_status=assignment.status, note=note,
    )
    assignment_changed(assignment)


def _create_assignment(incident, team, actor, note, supersedes=None):
    assignment = Assignment.objects.create(
        incident=incident, team=team, assigned_by=actor, note=note, supersedes=supersedes,
    )
    _team_status(team, ResponseTeam.Status.BUSY)
    _record(assignment, actor, AssignmentHistory.Operation.ASSIGN, "", note)
    if incident.status == IncidentStatus.VERIFIED:
        _incident_status(incident, IncidentStatus.DISPATCHED, actor, "Response team assigned.")
    return assignment


@transaction.atomic
def assign_team(*, actor, incident_id, team_id, note=""):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    incident = locked_object(Incident, incident_id)
    ensure_dispatchable(incident)
    team = locked_object(ResponseTeam, team_id)
    _validate_team(team, incident)
    return _create_assignment(incident, team, actor, note)


def _lock_assignment(actor, assignment_id, extra_team_id=None):
    try:
        snapshot = assignments_for(actor).only("incident_id", "team_id").get(pk=assignment_id)
    except Assignment.DoesNotExist as exc:
        raise NotFound("Assignment not found.") from exc
    incident = locked_object(Incident, snapshot.incident_id)
    ids = {snapshot.team_id}
    if extra_team_id is not None:
        ids.add(extra_team_id)
    teams = {team.pk: team for team in ResponseTeam.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
    if len(teams) != len(ids):
        raise NotFound("Response team not found.")
    assignment = locked_object(Assignment, assignment_id)
    return incident, teams, assignment


def _check_expected(assignment, expected_status):
    if assignment.status != expected_status:
        raise Conflict("Assignment status changed. Refresh before submitting again.")


def _finish(assignment, team, actor, target, operation, note):
    previous = assignment.status
    assignment.status = target
    assignment.ended_at = timezone.now()
    fields = ["status", "ended_at", "updated_at"]
    if target == Assignment.Status.COMPLETED:
        assignment.completed_at = assignment.ended_at
        fields.append("completed_at")
    assignment.save(update_fields=fields)
    _team_status(team, ResponseTeam.Status.AVAILABLE)
    _record(assignment, actor, operation, previous, note)


def _release_unserved_incident(incident, actor):
    if incident.status == IncidentStatus.DISPATCHED and not incident.assignments.filter(
        status__in=ACTIVE_ASSIGNMENT_STATUSES,
    ).exists():
        _incident_status(incident, IncidentStatus.VERIFIED, actor, "No active assignment remains; awaiting dispatch.")


@transaction.atomic
def cancel_assignment(*, actor, assignment_id, expected_status, note=""):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    incident, teams, assignment = _lock_assignment(actor, assignment_id)
    _check_expected(assignment, expected_status)
    if assignment.status not in ACTIVE_ASSIGNMENT_STATUSES:
        raise Conflict("Only an active assignment can be cancelled.")
    _finish(assignment, teams[assignment.team_id], actor, Assignment.Status.CANCELLED, AssignmentHistory.Operation.CANCEL, note)
    _release_unserved_incident(incident, actor)
    return assignment


@transaction.atomic
def reassign_team(*, actor, assignment_id, team_id, expected_status, note=""):
    require_role(actor, (Role.DISPATCHER, Role.ADMIN))
    incident, teams, assignment = _lock_assignment(actor, assignment_id, extra_team_id=team_id)
    _check_expected(assignment, expected_status)
    ensure_dispatchable(incident)
    if assignment.status not in ACTIVE_ASSIGNMENT_STATUSES:
        raise Conflict("Only an active assignment can be replaced.")
    if team_id == assignment.team_id:
        raise Conflict("Choose a different replacement team.")
    replacement_team = teams[team_id]
    _validate_team(replacement_team, incident)
    _finish(assignment, teams[assignment.team_id], actor, Assignment.Status.CANCELLED, AssignmentHistory.Operation.REASSIGN, note)
    # Do not temporarily revert incident status between cancelling and replacing.
    return _create_assignment(incident, replacement_team, actor, note, supersedes=assignment)


@transaction.atomic
def transition_assignment(*, actor, assignment_id, status, expected_status, note=""):
    require_role(actor, (Role.RESCUE_TEAM,))
    incident, teams, assignment = _lock_assignment(actor, assignment_id)
    _check_expected(assignment, expected_status)
    if status == assignment.status:
        return assignment  # Same-state request with a fresh expected state is a no-op.
    ensure_dispatchable(incident)
    if status not in RESCUE_TRANSITIONS.get(assignment.status, set()):
        raise Conflict("Invalid assignment transition.")
    team = teams[assignment.team_id]
    if status in TERMINAL_ASSIGNMENT_STATUSES:
        _finish(assignment, team, actor, status, AssignmentHistory.Operation.TRANSITION, note)
        _release_unserved_incident(incident, actor)
    else:
        previous = assignment.status
        assignment.status = status
        field = {"accepted": "accepted_at", "en_route": "en_route_at",
                 "on_scene": "arrived_at", "responding": "responding_at"}[status]
        setattr(assignment, field, timezone.now())
        assignment.save(update_fields=["status", field, "updated_at"])
        _record(assignment, actor, AssignmentHistory.Operation.TRANSITION, previous, note)
        if status == Assignment.Status.ON_SCENE and incident.status == IncidentStatus.DISPATCHED:
            _incident_status(incident, IncidentStatus.IN_PROGRESS, actor, "Response team arrived on scene.")
    return assignment


@transaction.atomic
def submit_signal(*, actor, assignment_id, data):
    from django.contrib.gis.geos import Point
    from realtime.events import publish_after_commit
    from .models import AssignmentSignal
    from .serializers import SignalCreateSerializer
    require_role(actor, (Role.RESCUE_TEAM,))
    serializer = SignalCreateSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    values = dict(serializer.validated_data)
    incident, teams, assignment = _lock_assignment(actor, assignment_id)
    latitude, longitude = values.pop("latitude", None), values.pop("longitude", None)
    values["location"] = Point(longitude, latitude, srid=4326) if latitude is not None else None
    existing = assignment.signals.filter(request_id=values["request_id"]).first()
    if existing:
        if any(getattr(existing, key) != value for key, value in values.items()):
            raise Conflict("Request ID already used with different data.")
        return existing
    ensure_dispatchable(incident)
    if assignment.status not in ACTIVE_ASSIGNMENT_STATUSES:
        raise Conflict("Field requests require an active assignment.")
    signal = AssignmentSignal.objects.create(assignment=assignment, actor=actor, **values)
    publish_after_commit(["dispatchers"], "assignment.signal_created", {
        "signal_id": signal.pk, "assignment_id": assignment.pk, "incident_id": incident.pk,
        "team_id": assignment.team_id, "kind": signal.kind,
    })
    return signal
