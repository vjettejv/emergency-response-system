from rest_framework import serializers

from incidents.serializers import CoordinateField, StrictSerializer, ZonedDateTimeField
from .models import ResponseTeam


class GPSUpdateSerializer(StrictSerializer):
    latitude = CoordinateField(min_value=-90, max_value=90)
    longitude = CoordinateField(min_value=-180, max_value=180)
    accuracy = CoordinateField(min_value=0, max_value=10000, required=False, allow_null=True, default=None)
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


class HistoryFilterSerializer(StrictSerializer):
    since = ZonedDateTimeField(required=False)
    until = ZonedDateTimeField(required=False)
    team = serializers.IntegerField(min_value=1, required=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False)


class RoutePointSerializer(StrictSerializer):
    latitude = CoordinateField(min_value=-90, max_value=90)
    longitude = CoordinateField(min_value=-180, max_value=180)


class RouteRequestSerializer(StrictSerializer):
    origin = RoutePointSerializer()
    destination = RoutePointSerializer()
