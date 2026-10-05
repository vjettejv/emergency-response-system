from django.conf import settings
from django.db import models
from django.utils import timezone
from django.contrib.gis.db.models import PointField

from common.models import TimestampedModel

ACTIVE_ASSIGNMENT_STATUSES = ("pending", "accepted", "en_route", "on_scene", "responding")
TERMINAL_ASSIGNMENT_STATUSES = ("completed", "rejected", "cancelled")


class Assignment(TimestampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        EN_ROUTE = "en_route", "En route"
        ON_SCENE = "on_scene", "On scene"
        RESPONDING = "responding", "Responding"
        COMPLETED = "completed", "Completed"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    incident = models.ForeignKey("incidents.Incident", on_delete=models.PROTECT, related_name="assignments")
    team = models.ForeignKey("teams.ResponseTeam", on_delete=models.PROTECT, related_name="assignments")
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="issued_assignments",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    note = models.TextField(blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(default=timezone.now)
    accepted_at = models.DateTimeField(null=True, blank=True)
    en_route_at = models.DateTimeField(null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    responding_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    supersedes = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="replacement",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team"],
                condition=models.Q(status__in=["pending", "accepted", "en_route", "on_scene", "responding"]),
                name="one_active_assignment_per_team",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=[
                    "pending", "accepted", "en_route", "on_scene", "responding", "completed", "rejected", "cancelled",
                ]), name="assignment_valid_status",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status__in=["pending", "accepted", "en_route", "on_scene", "responding"], ended_at__isnull=True)
                    | models.Q(status__in=["completed", "rejected", "cancelled"], ended_at__isnull=False)
                ), name="assignment_end_matches_status",
            ),
            models.CheckConstraint(
                condition=models.Q(ended_at__isnull=True) | models.Q(ended_at__gte=models.F("created_at")),
                name="assignment_end_after_creation",
            ),
        ]
        indexes = [models.Index(fields=["incident", "status"])]


class AssignmentHistory(models.Model):
    class Operation(models.TextChoices):
        ASSIGN = "assign", "Assign"
        TRANSITION = "transition", "Transition"
        CANCEL = "cancel", "Cancel"
        REASSIGN = "reassign", "Reassign"

    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT, related_name="history")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    operation = models.CharField(max_length=15, choices=Operation.choices)
    from_status = models.CharField(max_length=20, choices=Assignment.Status.choices, blank=True)
    to_status = models.CharField(max_length=20, choices=Assignment.Status.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(operation__in=["assign", "transition", "cancel", "reassign"]),
                name="assignment_history_operation",
            ),
            models.CheckConstraint(
                condition=models.Q(from_status="") | models.Q(from_status__in=Assignment.Status.values),
                name="assignment_history_from_status",
            ),
            models.CheckConstraint(
                condition=models.Q(to_status__in=Assignment.Status.values), name="assignment_history_to_status",
            ),
            models.CheckConstraint(
                condition=~models.Q(from_status=models.F("to_status")), name="assignment_history_changed",
            ),
        ]


class AssignmentSignal(models.Model):
    """A persisted field issue/support request, never an automatic dispatch command."""
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT, related_name="signals")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=15, choices=[("problem", "Problem"), ("support", "Support")])
    problem = models.CharField(max_length=30, blank=True)
    support_type = models.CharField(max_length=15, blank=True)
    note = models.TextField(blank=True)
    location = PointField(srid=4326, geography=True, null=True, blank=True)
    accuracy = models.FloatField(null=True, blank=True)
    reported_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    request_id = models.UUIDField()

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [models.UniqueConstraint(fields=["assignment", "request_id"], name="unique_assignment_signal_request")]
