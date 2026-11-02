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


class GPSJourney(models.Model):
    """Observed tracking session; gaps and assignment changes start a new journey."""
    team = models.ForeignKey(ResponseTeam, on_delete=models.PROTECT, related_name="journeys")
    assignment = models.ForeignKey("dispatch.Assignment", on_delete=models.PROTECT, null=True, blank=True, related_name="journeys")
    started_at = models.DateTimeField()
    last_recorded_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at", "-pk"]
        indexes = [models.Index(fields=["team", "started_at"], name="journey_team_started_idx")]
        constraints = [
            models.UniqueConstraint(fields=["team"], condition=models.Q(ended_at__isnull=True), name="one_open_journey_per_team"),
            models.CheckConstraint(condition=models.Q(last_recorded_at__gte=models.F("started_at")), name="journey_recorded_after_start"),
            models.CheckConstraint(condition=models.Q(ended_at__isnull=True) | models.Q(ended_at__gte=models.F("started_at")), name="journey_end_after_start"),
        ]


class GPSPoint(models.Model):
    journey = models.ForeignKey(GPSJourney, on_delete=models.PROTECT, related_name="points")
    location = models.PointField(srid=4326, geography=True)
    recorded_at = models.DateTimeField()
    received_at = models.DateTimeField()
    accuracy = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["recorded_at", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["journey", "recorded_at"], name="unique_journey_sample_time"),
            models.CheckConstraint(condition=models.Q(accuracy__isnull=True) | models.Q(accuracy__gte=0, accuracy__lte=10000), name="gps_point_accuracy_range"),
        ]
