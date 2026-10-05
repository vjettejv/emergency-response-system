import math
from collections.abc import Mapping

from django.contrib.gis.geos import Point
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers
from common.serializers import PhoneField

from .models import (
    DuplicateDismissal, Incident, IncidentCategory, IncidentReport,
    IncidentReportLinkHistory, IncidentStatus, IncidentStatusHistory,
)


class StrictSerializer(serializers.Serializer):
    """Reject misspelled and server-owned input fields instead of ignoring them."""

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({key: "Unknown field." for key in sorted(unknown)})
        return super().to_internal_value(data)


class CoordinateField(serializers.FloatField):
    def to_internal_value(self, data):
        if isinstance(data, bool):
            self.fail("invalid")
        value = super().to_internal_value(data)
        if not math.isfinite(value):
            self.fail("invalid")
        return value


class ZonedDateTimeField(serializers.DateTimeField):
    def to_internal_value(self, value):
        try:
            parsed = parse_datetime(value) if isinstance(value, str) else value
        except ValueError:
            parsed = None
        if not hasattr(parsed, "utcoffset") or timezone.is_naive(parsed):
            raise serializers.ValidationError("Use an ISO 8601 timestamp with Z or a timezone offset.")
        return super().to_internal_value(value)


class MediaMetadataSerializer(StrictSerializer):
    filename = serializers.CharField(max_length=255)
    content_type = serializers.ChoiceField(choices=[
        "image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm", "audio/mpeg", "audio/wav",
    ])
    size_bytes = serializers.IntegerField(min_value=1, max_value=50 * 1024 * 1024)


class GPSLocationSerializer(StrictSerializer):
    latitude = CoordinateField(min_value=-90, max_value=90)
    longitude = CoordinateField(min_value=-180, max_value=180)

    def validate(self, attrs):
        return Point(attrs["longitude"], attrs["latitude"], srid=4326)


class ReportCreateSerializer(StrictSerializer):
    reporter_name = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    reporter_phone = PhoneField(max_length=30, required=False, allow_blank=True, default="")
    allow_contact = serializers.BooleanField(required=False, default=True)
    gps_location = GPSLocationSerializer(required=False, allow_null=True, default=None)
    location_accuracy = CoordinateField(min_value=0, required=False, allow_null=True)
    category = serializers.PrimaryKeyRelatedField(queryset=IncidentCategory.objects.filter(is_active=True))
    description = serializers.CharField(max_length=10000)
    latitude = CoordinateField(min_value=-90, max_value=90)
    longitude = CoordinateField(min_value=-180, max_value=180)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    occurred_at = ZonedDateTimeField(required=False, allow_null=True)
    media_metadata = MediaMetadataSerializer(many=True, required=False, max_length=10)

    def validate_occurred_at(self, value):
        if value is not None and value > timezone.now():
            raise serializers.ValidationError("Occurrence time cannot be in the future.")
        return value

    def validate(self, attrs):
        actor = getattr(self.context.get("request"), "user", None)
        if actor:
            if "reporter_name" not in self.initial_data:
                attrs["reporter_name"] = actor.get_full_name().strip() or actor.username
            if "reporter_phone" not in self.initial_data:
                attrs["reporter_phone"] = PhoneField().run_validation(actor.phone) if actor.phone else ""
        errors = {key: "Required for emergency contact." for key in ("reporter_name", "reporter_phone") if not attrs.get(key)}
        if errors:
            raise serializers.ValidationError(errors)
        # GeoDjango expects x=longitude, y=latitude, never the reverse.
        attrs["location"] = Point(attrs.pop("longitude"), attrs.pop("latitude"), srid=4326)
        return attrs


class DraftReportSerializer(ReportCreateSerializer):
    request_id = serializers.UUIDField()


class ReportSerializer(serializers.ModelSerializer):
    reporter_phone = serializers.SerializerMethodField()

    def get_reporter_phone(self, obj):
        from .contacts import masked_phone
        return masked_phone(obj.reporter_phone)

    incident_status = serializers.CharField(source="incident.status", read_only=True, default=None)
    latitude = serializers.FloatField(source="location.y", read_only=True)
    longitude = serializers.FloatField(source="location.x", read_only=True)
    reported_at = serializers.SerializerMethodField()

    def get_reported_at(self, obj):
        return None if obj.is_draft else (obj.submitted_at or obj.created_at).isoformat()

    class Meta:
        model = IncidentReport
        fields = (
            "id", "is_draft", "reporter", "reporter_name", "reporter_phone", "allow_contact", "location_accuracy", "category", "description", "latitude", "longitude", "address",
            "occurred_at", "reported_at", "updated_at", "media_metadata", "review_status",
            "reviewed_by", "reviewed_at", "review_note", "incident", "incident_status",
        )
        read_only_fields = fields


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentCategory
        fields = ("id", "code", "name", "description")
        read_only_fields = fields


class IncidentSerializer(serializers.ModelSerializer):
    latitude = serializers.FloatField(source="location.y", read_only=True)
    longitude = serializers.FloatField(source="location.x", read_only=True)

    class Meta:
        model = Incident
        fields = (
            "id", "title", "description", "category", "latitude", "longitude", "address",
            "status", "created_by", "created_at", "updated_at",
        )
        read_only_fields = fields


class HistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentStatusHistory
        fields = ("id", "incident", "from_status", "to_status", "changed_by", "changed_at", "note")
        read_only_fields = fields


class ReportReviewSerializer(StrictSerializer):
    review_status = serializers.ChoiceField(choices=[
        IncidentReport.ReviewStatus.ACCEPTED, IncidentReport.ReviewStatus.REJECTED,
    ])
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class IncidentFromReportSerializer(StrictSerializer):
    title = serializers.CharField(max_length=200)


class LinkReportsSerializer(StrictSerializer):
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    report_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=100,
    )

    def validate_report_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Duplicate report IDs are not allowed.")
        return value


class CandidateReportSerializer(ReportSerializer):
    distance_meters = serializers.FloatField(read_only=True)

    class Meta(ReportSerializer.Meta):
        fields = ReportSerializer.Meta.fields + ("distance_meters",)
        read_only_fields = fields


class CandidateIncidentSerializer(IncidentSerializer):
    distance_meters = serializers.FloatField(read_only=True)
    matched_report_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta(IncidentSerializer.Meta):
        fields = IncidentSerializer.Meta.fields + ("distance_meters", "matched_report_id")
        read_only_fields = fields


class PotentialDuplicatesSerializer(serializers.Serializer):
    source_report_id = serializers.IntegerField()
    radius_meters = serializers.IntegerField()
    time_window_minutes = serializers.IntegerField()
    effective_at = serializers.DateTimeField()
    reports = CandidateReportSerializer(many=True)
    incidents = CandidateIncidentSerializer(many=True)
    more_reports = serializers.BooleanField()
    more_incidents = serializers.BooleanField()


class DismissDuplicateSerializer(StrictSerializer):
    candidate_report_id = serializers.IntegerField(min_value=1, required=False)
    incident_id = serializers.IntegerField(min_value=1, required=False)
    reason = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if ("candidate_report_id" in attrs) == ("incident_id" in attrs):
            raise serializers.ValidationError("Specify exactly one candidate_report_id or incident_id.")
        return attrs


class DuplicateDismissalSerializer(serializers.ModelSerializer):
    class Meta:
        model = DuplicateDismissal
        fields = ("id", "report", "candidate_report", "incident", "dismissed_by", "created_at", "reason")
        read_only_fields = fields


class ReportLinkHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentReportLinkHistory
        fields = ("id", "incident", "report", "actor", "operation", "created_at", "note")
        read_only_fields = fields


class IncidentStatusSerializer(StrictSerializer):
    status = serializers.ChoiceField(choices=IncidentStatus.choices)
    expected_status = serializers.ChoiceField(choices=IncidentStatus.choices)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class ListQuerySerializer(serializers.Serializer):
    # Pagination parameters are handled by DRF, so unknown query keys are ignored.
    status = serializers.CharField(required=False)
    category = serializers.IntegerField(min_value=1, required=False)
    created_after = ZonedDateTimeField(required=False)
    created_before = ZonedDateTimeField(required=False)
    search = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate(self, attrs):
        start, end = attrs.get("created_after"), attrs.get("created_before")
        if start and end and start > end:
            raise serializers.ValidationError({"created_before": "Must be on or after created_after."})
        return attrs
