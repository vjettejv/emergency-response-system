import re

from rest_framework import serializers


class PhoneField(serializers.CharField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not value:
            return value
        value = re.sub(r"[ .()-]", "", value)
        if not re.fullmatch(r"\+?[0-9]{8,15}", value):
            raise serializers.ValidationError("Use 8–15 digits, optionally starting with +.")
        return value
