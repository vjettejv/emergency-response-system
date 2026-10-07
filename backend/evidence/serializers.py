import base64
import binascii
from pathlib import PurePosixPath

from django.conf import settings
from rest_framework import serializers
from incidents.serializers import StrictSerializer, CoordinateField, ZonedDateTimeField
from django.utils import timezone
from .models import MediaAsset

FILE_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"}, "image/png": {".png"}, "image/webp": {".webp"},
    "video/mp4": {".mp4"}, "video/webm": {".webm"}, "audio/mpeg": {".mp3"}, "audio/wav": {".wav"},
}


class ParentSerializer(StrictSerializer):
    report_id = serializers.IntegerField(min_value=1, required=False)
    incident_id = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        if ("report_id" in attrs) == ("incident_id" in attrs):
            raise serializers.ValidationError("Specify exactly one report_id or incident_id.")
        return attrs


class UploadSerializer(ParentSerializer):
    capture_source = serializers.ChoiceField(choices=["upload", "camera"], required=False, default="upload")
    captured_at = ZonedDateTimeField(required=False, allow_null=True)
    capture_latitude = CoordinateField(min_value=-90, max_value=90, required=False, allow_null=True)
    capture_longitude = CoordinateField(min_value=-180, max_value=180, required=False, allow_null=True)
    capture_accuracy = CoordinateField(min_value=0, required=False, allow_null=True)
    filename = serializers.CharField(max_length=255)
    content_type = serializers.ChoiceField(choices=list(FILE_TYPES))
    size_bytes = serializers.IntegerField(min_value=1)
    checksum_sha256 = serializers.CharField(max_length=44)

    def validate(self, attrs):
        super().validate(attrs)
        captured = attrs.get("captured_at")
        if attrs["capture_source"] == "camera" and (not captured or not attrs["content_type"].startswith("image/")):
            raise serializers.ValidationError("Camera metadata requires a capture timestamp and image content type.")
        if captured and (attrs["capture_source"] != "camera" or captured > timezone.now()):
            raise serializers.ValidationError("Invalid capture timestamp/source.")
        if (attrs.get("capture_latitude") is None) != (attrs.get("capture_longitude") is None):
            raise serializers.ValidationError("Capture position requires both coordinates.")
        if attrs.get("capture_accuracy") is not None and attrs.get("capture_latitude") is None:
            raise serializers.ValidationError("Capture accuracy requires a device position.")
        if attrs["capture_source"] != "camera" and any(attrs.get(key) is not None for key in ("capture_latitude", "capture_longitude", "capture_accuracy")):
            raise serializers.ValidationError("Capture position requires the camera flow.")
        name = attrs["filename"]
        if any(ord(c) < 32 for c in name) or any(c in name for c in ("/", "\\", ":")) or PurePosixPath(name).suffix.lower() not in FILE_TYPES[attrs["content_type"]]:
            raise serializers.ValidationError({"filename": "Extension must match the allowed content type; use a filename without paths."})
        if attrs["size_bytes"] > settings.MEDIA_MAX_BYTES:
            raise serializers.ValidationError({"size_bytes": "File exceeds the configured size limit."})
        try:
            digest = base64.b64decode(attrs["checksum_sha256"], validate=True)
            if len(digest) != 32 or base64.b64encode(digest).decode() != attrs["checksum_sha256"]:
                raise ValueError
        except (ValueError, binascii.Error):
            raise serializers.ValidationError({"checksum_sha256": "Use base64-encoded SHA-256 of the file bytes."})
        return attrs


class MediaSerializer(serializers.ModelSerializer):
    media_type = serializers.SerializerMethodField()

    def get_media_type(self, obj):
        return obj.content_type.split("/")[0]
    class Meta:
        model = MediaAsset
        fields = ("id", "media_type", "report", "incident", "uploaded_by", "filename", "content_type", "size_bytes",
                  "capture_source", "captured_at", "capture_latitude", "capture_longitude", "capture_accuracy",
                  "status", "created_at", "upload_expires_at", "confirmed_at", "cleanup_attempts", "cleanup_error",
                  "processing_status", "optimized_size", "width", "height")
        read_only_fields = fields
