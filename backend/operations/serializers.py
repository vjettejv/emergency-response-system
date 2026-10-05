from collections.abc import Mapping

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from accounts.models import Role, User
from incidents.models import IncidentCategory
from incidents.serializers import CoordinateField
from teams.models import ResponseTeam


class StrictModelSerializer(serializers.ModelSerializer):
    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({key: "Unknown field." for key in unknown})
        return super().to_internal_value(data)


class ManagedUserSerializer(StrictModelSerializer):
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "role", "response_team", "is_active", "password"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        if not self.instance and not attrs.get("password"):
            raise serializers.ValidationError({"password": "Required for a new account."})
        role = attrs.get("role", self.instance.role if self.instance else Role.CITIZEN)
        team = attrs.get("response_team", self.instance.response_team if self.instance else None)
        if role != Role.RESCUE_TEAM and team:
            # Explicit role changes can clear the old membership.
            if "response_team" in attrs or "role" not in attrs:
                raise serializers.ValidationError({"response_team": "Only rescue accounts can belong to a team."})
            attrs["response_team"] = None
        if "password" in attrs:
            candidate = self.instance or User(username=attrs.get("username", ""), email=attrs.get("email", ""))
            validate_password(attrs["password"], candidate)
        return attrs


class CategorySerializer(StrictModelSerializer):
    class Meta:
        model = IncidentCategory
        fields = ["id", "name", "code", "description", "is_active"]
        read_only_fields = ["id"]


class TeamSerializer(StrictModelSerializer):
    latitude = CoordinateField(min_value=-90, max_value=90, required=False, write_only=True)
    longitude = CoordinateField(min_value=-180, max_value=180, required=False, write_only=True)
    location = serializers.SerializerMethodField()

    class Meta:
        model = ResponseTeam
        fields = ["id", "name", "code", "categories", "status", "latitude", "longitude", "location", "location_updated_at"]
        read_only_fields = ["id", "location_updated_at"]

    def get_location(self, obj):
        return {"latitude": obj.last_location.y, "longitude": obj.last_location.x} if obj.last_location else None

    def validate(self, attrs):
        if ("latitude" in attrs) != ("longitude" in attrs):
            raise serializers.ValidationError("Provide both latitude and longitude.")
        return attrs
