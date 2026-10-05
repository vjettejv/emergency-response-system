from django.contrib.gis.db import models

from common.models import TimestampedModel


class ResponseTeam(TimestampedModel):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        BUSY = "busy", "Busy"
        OFFLINE = "offline", "Offline"

    name = models.CharField(max_length=150)
    code = models.SlugField(max_length=50, unique=True)
    categories = models.ManyToManyField("incidents.IncidentCategory", related_name="response_teams", blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OFFLINE)
    last_location = models.PointField(srid=4326, geography=True, null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)
    location_accuracy = models.FloatField(null=True, blank=True)
    location_received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status"], name="team_readiness_idx")]
        constraints = [models.CheckConstraint(
            condition=models.Q(status__in=["available", "busy", "offline"]), name="team_valid_status",
        ), models.CheckConstraint(
            condition=models.Q(location_accuracy__isnull=True) | models.Q(location_accuracy__gte=0, location_accuracy__lte=10000),
            name="team_location_accuracy_range",
        )]

    def __str__(self):
        return self.name
