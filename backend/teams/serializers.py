from rest_framework import serializers

from incidents.serializers import CoordinateField, StrictSerializer, ZonedDateTimeField
from .models import ResponseTeam


class GPSUpdateSerializer(StrictSerializer):
    latitude = CoordinateField(min_value=-90, max_value=90)
    longitude = CoordinateField(min_value=-180, max_value=180)
    accuracy = CoordinateField(min_value=0, max_value=10000)
    timestamp = ZonedDateTimeField()


class TeamLocationSerializer(serializers.ModelSerializer):
    team_id = serializers.IntegerField(source="pk", read_only=True)
    latitude = serializers.FloatField(source="last_location.y", read_only=True, default=None)
    longitude = serializers.FloatField(source="last_location.x", read_only=True, default=None)
    accuracy = serializers.FloatField(source="location_accuracy", read_only=True, allow_null=True)
    timestamp = serializers.DateTimeField(source="location_updated_at", read_only=True, allow_null=True)
    received_at = serializers.DateTimeField(source="location_received_at", read_only=True, allow_null=True)

    class Meta:
        model = ResponseTeam
        fields = ("team_id", "name", "status", "latitude", "longitude", "accuracy", "timestamp", "received_at")
        read_only_fields = fields
