from django.conf import settings
from django.contrib.gis.db import models
from django.db.models.functions import Coalesce

from common.models import TimestampedModel


class IncidentStatus(models.TextChoices):
    NEW = "new", "New"
    VERIFIED = "verified", "Verified"
    DISPATCHED = "dispatched", "Dispatched"
    IN_PROGRESS = "in_progress", "In progress"
    RESOLVED = "resolved", "Resolved"
    CANCELLED = "cancelled", "Cancelled"


class IncidentCategory(TimestampedModel):
    name = models.CharField(max_length=120)
    code = models.SlugField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "incident categories"

    def __str__(self):
        return self.name


class Incident(TimestampedModel):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.ForeignKey(IncidentCategory, on_delete=models.PROTECT, related_name="incidents")
    location = models.PointField(srid=4326, geography=True)
    address = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=IncidentStatus.choices, default=IncidentStatus.NEW)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_incidents",
    )

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["category", "created_at"], name="incident_cluster_time_idx"),
        ]
        constraints = [models.CheckConstraint(
            condition=models.Q(status__in=IncidentStatus.values), name="incident_valid_status",
        )]

    def __str__(self):
        return self.title


class IncidentReport(TimestampedModel):
    class ReviewStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="incident_reports",
    )
    incident = models.ForeignKey(
        Incident, on_delete=models.PROTECT, null=True, blank=True, related_name="reports",
    )
    category = models.ForeignKey(
        IncidentCategory, on_delete=models.PROTECT, related_name="reports",
    )
    description = models.TextField()
    reporter_name = models.CharField(max_length=120, blank=True)
    reporter_phone = models.CharField(max_length=16, blank=True)
    allow_contact = models.BooleanField(default=False)
    location_accuracy = models.FloatField(null=True, blank=True)
    is_draft = models.BooleanField(default=False)
    draft_request_id = models.UUIDField(null=True, blank=True, unique=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    location = models.PointField(srid=4326, geography=True)
    address = models.CharField(max_length=500, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    media_metadata = models.JSONField(default=list, blank=True)
    review_status = models.CharField(max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_incident_reports",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["review_status", "created_at"]),
            models.Index(
                models.F("category"), models.F("review_status"), Coalesce("occurred_at", "submitted_at", "created_at"),
                name="report_cluster_time_idx",
            ),
        ]
        constraints = [models.CheckConstraint(
            condition=models.Q(review_status__in=["pending", "accepted", "rejected"]),
            name="report_valid_review_status",
        )]


class IncidentStatusHistory(models.Model):
    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, related_name="status_history")
    from_status = models.CharField(max_length=20, choices=IncidentStatus.choices, blank=True)
    to_status = models.CharField(max_length=20, choices=IncidentStatus.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="incident_status_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["changed_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(from_status="") | models.Q(from_status__in=IncidentStatus.values),
                name="history_valid_from_status",
            ),
            models.CheckConstraint(
                condition=models.Q(to_status__in=IncidentStatus.values), name="history_valid_to_status",
            ),
            models.CheckConstraint(
                condition=~models.Q(from_status=models.F("to_status")), name="history_status_changed",
            ),
        ]


class DuplicateDismissal(models.Model):
    """Persistent dispatcher decision; report pairs are stored in ascending ID order."""

    report = models.ForeignKey(IncidentReport, on_delete=models.PROTECT, related_name="duplicate_dismissals")
    candidate_report = models.ForeignKey(
        IncidentReport, on_delete=models.PROTECT, null=True, blank=True, related_name="dismissed_by_reports",
    )
    incident = models.ForeignKey(
        Incident, on_delete=models.PROTECT, null=True, blank=True, related_name="duplicate_dismissals",
    )
    dismissed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(candidate_report__isnull=False, incident__isnull=True)
                    | models.Q(candidate_report__isnull=True, incident__isnull=False)
                ), name="dismissal_exactly_one_target",
            ),
            models.CheckConstraint(
                condition=models.Q(candidate_report__isnull=True) | models.Q(report__lt=models.F("candidate_report")),
                name="dismissal_report_pair_order",
            ),
            models.UniqueConstraint(
                fields=["report", "candidate_report"], condition=models.Q(candidate_report__isnull=False),
                name="unique_dismissed_report_pair",
            ),
            models.UniqueConstraint(
                fields=["report", "incident"], condition=models.Q(incident__isnull=False),
                name="unique_dismissed_incident",
            ),
        ]


class IncidentReportLinkHistory(models.Model):
    class Operation(models.TextChoices):
        CREATE = "create", "Create incident from report"
        LINK = "link", "Link report"
        MERGE = "merge", "Merge reports into incident"

    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, related_name="report_link_history")
    report = models.ForeignKey(IncidentReport, on_delete=models.PROTECT, related_name="link_history")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    operation = models.CharField(max_length=10, choices=Operation.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [models.CheckConstraint(
            condition=models.Q(operation__in=["create", "link", "merge"]), name="report_link_valid_operation",
        )]


class ContactAccess(models.Model):
    """Explicit administrative access; never copy contact values into the audit log."""
    report = models.ForeignKey(IncidentReport, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    purpose = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
