import uuid

from django.conf import settings
from django.db import models
from common.models import TimestampedModel


class MediaAsset(TimestampedModel):
    class ProcessingStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", "Original only"
        PENDING = "pending", "Awaiting optimization"
        PROCESSING = "processing", "Optimizing"
        READY = "ready", "Optimized"
        FAILED = "failed", "Optimization failed"

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting upload"
        READY = "ready", "Confirmed"
        DELETING = "deleting", "Awaiting S3 cleanup"
        DELETED = "deleted", "Deleted"
        FAILED = "failed", "Cleanup failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey("incidents.IncidentReport", null=True, blank=True, on_delete=models.PROTECT, related_name="media_assets")
    incident = models.ForeignKey("incidents.Incident", null=True, blank=True, on_delete=models.PROTECT, related_name="media_assets")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="media_assets")
    filename = models.CharField(max_length=255)
    capture_source = models.CharField(max_length=20, default="upload", choices=[("upload", "Upload"), ("camera", "Client camera flow")])
    captured_at = models.DateTimeField(null=True, blank=True)
    capture_latitude = models.FloatField(null=True, blank=True)
    capture_longitude = models.FloatField(null=True, blank=True)
    capture_accuracy = models.FloatField(null=True, blank=True)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    checksum_sha256 = models.CharField(max_length=44)
    bucket = models.CharField(max_length=255)
    object_key = models.CharField(max_length=255, unique=True)
    version_id = models.CharField(max_length=1024, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    upload_expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="deleted_media")
    cleanup_attempts = models.PositiveSmallIntegerField(default=0)
    cleanup_error = models.CharField(max_length=40, blank=True)
    next_cleanup_at = models.DateTimeField()
    # object_key, size_bytes and version_id always describe the private original.
    optimized_key = models.CharField(max_length=255, blank=True)
    optimized_version_id = models.CharField(max_length=1024, blank=True)
    optimized_size = models.PositiveBigIntegerField(null=True, blank=True)
    optimized_content_type = models.CharField(max_length=100, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    processing_status = models.CharField(max_length=16, choices=ProcessingStatus.choices, default=ProcessingStatus.NOT_REQUIRED)
    processing_attempts = models.PositiveSmallIntegerField(default=0)
    processing_error = models.CharField(max_length=40, blank=True)
    next_processing_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "pk"]
        indexes = [models.Index(fields=["status", "next_cleanup_at"], name="media_cleanup_due"),
                   models.Index(fields=["processing_status", "next_processing_at"], name="media_processing_due")]
        constraints = [
            models.CheckConstraint(condition=(models.Q(report__isnull=False, incident__isnull=True) | models.Q(report__isnull=True, incident__isnull=False)), name="media_exactly_one_parent"),
            models.CheckConstraint(condition=models.Q(size_bytes__gt=0), name="media_positive_size"),
            models.CheckConstraint(condition=models.Q(status__in=["pending", "ready", "deleting", "deleted", "failed"]), name="media_valid_status"),
            models.CheckConstraint(condition=~models.Q(status="ready") | models.Q(confirmed_at__isnull=False), name="media_ready_confirmed"),
        ]
