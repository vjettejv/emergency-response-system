from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Type(models.TextChoices):
        NEW_REPORT = "new_report", "New report"
        REPORT_RECEIVED = "report_received", "Report received"
        REPORT_VERIFIED = "report_verified", "Report verified"
        REPORT_REJECTED = "report_rejected", "Report rejected"
        INCIDENT_CREATED = "incident_created", "Incident created"
        INCIDENT_STATUS_CHANGED = "incident_status_changed", "Incident changed"
        INCIDENT_RESOLVED = "incident_resolved", "Incident resolved"
        NEW_ASSIGNMENT = "new_assignment", "New assignment"
        ASSIGNMENT_CANCELLED = "assignment_cancelled", "Assignment cancelled"
        ASSIGNMENT_ACCEPTED = "assignment_accepted", "Assignment accepted"
        ASSIGNMENT_REJECTED = "assignment_rejected", "Assignment rejected"
        TEAM_ARRIVED = "team_arrived", "Team arrived"
        ASSIGNMENT_COMPLETED = "assignment_completed", "Assignment completed"
        ASSISTANCE_REQUESTED = "assistance_requested", "Assistance requested"

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    type = models.CharField(max_length=32, choices=Type.choices)
    title = models.CharField(max_length=120)
    message = models.CharField(max_length=240)
    event_key = models.CharField(max_length=160)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    report = models.ForeignKey("incidents.IncidentReport", null=True, blank=True, on_delete=models.SET_NULL)
    incident = models.ForeignKey("incidents.Incident", null=True, blank=True, on_delete=models.SET_NULL)
    assignment = models.ForeignKey("dispatch.Assignment", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["recipient", "read_at", "created_at"], name="notification_inbox_idx")]
        constraints = [
            models.UniqueConstraint(fields=["recipient", "event_key"], name="notification_recipient_event"),
            models.CheckConstraint(condition=models.Q(type__in=[
                "new_report", "report_received", "report_verified", "report_rejected", "incident_created",
                "incident_status_changed", "incident_resolved", "new_assignment", "assignment_cancelled",
                "assignment_accepted", "assignment_rejected", "team_arrived", "assignment_completed", "assistance_requested",
            ]), name="notification_valid_type"),
        ]
