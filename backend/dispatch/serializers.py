from rest_framework import serializers

from incidents.serializers import IncidentSerializer, StrictSerializer, CoordinateField, ZonedDateTimeField
from django.utils import timezone
from datetime import timedelta
from teams.models import ResponseTeam

from .models import Assignment, AssignmentHistory, AssignmentSignal


class AssignmentStateField(serializers.ChoiceField):
    """Use assigned in API contracts without rewriting legacy pending rows."""

    def __init__(self, **kwargs):
        super().__init__(choices=[
            "assigned", "accepted", "en_route", "on_scene", "responding", "completed", "rejected", "cancelled",
        ], **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        return Assignment.Status.PENDING if value == "assigned" else value

    def to_representation(self, value):
        return "assigned" if value == Assignment.Status.PENDING else value


class TeamSuggestionSerializer(serializers.ModelSerializer):
    latitude = serializers.FloatField(source="last_location.y", read_only=True)
    longitude = serializers.FloatField(source="last_location.x", read_only=True)
    distance_meters = serializers.FloatField(read_only=True)
    eta_seconds = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = ResponseTeam
        fields = (
            "id", "code", "name", "status", "latitude", "longitude", "location_updated_at",
            "distance_meters", "eta_seconds",
        )
        read_only_fields = fields


class SuggestionsSerializer(serializers.Serializer):
    incident_id = serializers.IntegerField()
    radius_meters = serializers.IntegerField()
    ranking = serializers.CharField()
    routing_available = serializers.BooleanField()
    more_teams = serializers.BooleanField()
    teams = TeamSuggestionSerializer(many=True)


class AssignmentSerializer(serializers.ModelSerializer):
    status = AssignmentStateField(read_only=True)
    incident = IncidentSerializer(read_only=True)
    timeline = serializers.SerializerMethodField()

    def get_timeline(self, obj):
        from .timeline import assignment_timeline
        return assignment_timeline(obj)

    class Meta:
        model = Assignment
        fields = (
            "id", "timeline", "incident", "team", "assigned_by", "status", "note", "supersedes",
            "created_at", "updated_at", "ended_at", "dispatched_at", "accepted_at",
            "en_route_at", "arrived_at", "responding_at", "completed_at",
        )
        read_only_fields = fields


class AssignmentHistorySerializer(serializers.ModelSerializer):
    from_status = AssignmentStateField(read_only=True, allow_blank=True)
    to_status = AssignmentStateField(read_only=True)

    class Meta:
        model = AssignmentHistory
        fields = ("id", "assignment", "actor", "operation", "from_status", "to_status", "created_at", "note")
        read_only_fields = fields


class AssignTeamSerializer(StrictSerializer):
    team_id = serializers.IntegerField(min_value=1)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class ExpectedStateSerializer(StrictSerializer):
    expected_status = AssignmentStateField()
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class ReassignSerializer(ExpectedStateSerializer):
    team_id = serializers.IntegerField(min_value=1)


class TransitionSerializer(ExpectedStateSerializer):
    status = AssignmentStateField()


class AssignmentQuerySerializer(serializers.Serializer):
    incident = serializers.IntegerField(min_value=1, required=False)
    team = serializers.IntegerField(min_value=1, required=False)
    status = AssignmentStateField(required=False)


class SignalCreateSerializer(StrictSerializer):
    request_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=["problem", "support"])
    problem = serializers.ChoiceField(choices=["missing_location", "unreachable_contact", "blocked_road", "extra_forces", "more_severe"], required=False, default="")
    support_type = serializers.ChoiceField(choices=["fire", "medical", "rescue", "other"], required=False, default="")
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    latitude = CoordinateField(min_value=-90, max_value=90, required=False)
    longitude = CoordinateField(min_value=-180, max_value=180, required=False)
    accuracy = CoordinateField(min_value=0, required=False, allow_null=True)
    reported_at = ZonedDateTimeField()

    def validate(self, attrs):
        if ("latitude" in attrs) != ("longitude" in attrs):
            raise serializers.ValidationError("Provide both coordinates.")
        if attrs.get("accuracy") is not None and "latitude" not in attrs:
            raise serializers.ValidationError("Accuracy requires coordinates.")
        if attrs["kind"] == "support":
            if not attrs["support_type"] or attrs["problem"] or "latitude" not in attrs:
                raise serializers.ValidationError("Support requests require a support type and current position.")
        elif not attrs["problem"] or attrs["support_type"]:
            raise serializers.ValidationError("Problem requests require a problem type only.")
        if not timezone.now() - timedelta(minutes=5) <= attrs["reported_at"] <= timezone.now() + timedelta(seconds=30):
            raise serializers.ValidationError({"reported_at": "Use a current device timestamp (within five minutes)."})
        return attrs


class SignalSerializer(serializers.ModelSerializer):
    latitude = serializers.FloatField(source="location.y", default=None, read_only=True)
    longitude = serializers.FloatField(source="location.x", default=None, read_only=True)

    class Meta:
        model = AssignmentSignal
        fields = ("id", "assignment", "actor", "request_id", "kind", "problem", "support_type", "note",
                  "latitude", "longitude", "accuracy", "reported_at", "created_at")
        read_only_fields = fields
