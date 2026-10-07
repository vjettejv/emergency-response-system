from rest_framework import serializers
from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    related_entity = serializers.SerializerMethodField()

    def get_related_entity(self, obj):
        for kind in ("assignment", "report", "incident"):
            value = getattr(obj, kind + "_id")
            if value:
                return {"kind": kind, "id": value}
        return None

    class Meta:
        model = Notification
        fields = ("id", "type", "title", "message", "created_at", "read_at", "related_entity")
        read_only_fields = fields
