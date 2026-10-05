from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from common.serializers import PhoneField

from .models import Role, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "phone", "role", "response_team")
        read_only_fields = fields


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    phone = PhoneField(max_length=30, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ("username", "email", "password", "first_name", "last_name", "phone")

    def validate(self, attrs):
        user = User(**{key: value for key, value in attrs.items() if key != "password"})
        try:
            validate_password(attrs["password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": exc.messages}) from exc
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data, role=Role.CITIZEN)


class RoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=Role.choices)
