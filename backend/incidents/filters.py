from django.db.models import Q
from django.db.models.functions import Coalesce
from rest_framework.exceptions import ValidationError
from rest_framework.filters import BaseFilterBackend

from .models import IncidentReport, IncidentStatus
from .serializers import ListQuerySerializer


class IncidentDataFilter(BaseFilterBackend):
    """Small explicit filters using the existing ORM; no extra filtering dependency."""

    def filter_queryset(self, request, queryset, view):
        query = ListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        is_report = queryset.model is IncidentReport
        status_field = "review_status" if is_report else "status"
        statuses = IncidentReport.ReviewStatus.values if is_report else IncidentStatus.values
        if "status" in params:
            if params["status"] not in statuses:
                raise ValidationError({"status": "Invalid status for this resource."})
            queryset = queryset.filter(**{status_field: params["status"]})
        if "category" in params:
            queryset = queryset.filter(category_id=params["category"])
        time_field = "created_at"
        if is_report:
            queryset = queryset.alias(received_time=Coalesce("submitted_at", "created_at"))
            time_field = "received_time"
        if "created_after" in params:
            queryset = queryset.filter(**{f"{time_field}__gte": params["created_after"]})
        if "created_before" in params:
            queryset = queryset.filter(**{f"{time_field}__lte": params["created_before"]})
        if params.get("search"):
            term = params["search"]
            search = Q(description__icontains=term) | Q(address__icontains=term)
            if not is_report:
                search |= Q(title__icontains=term)
            queryset = queryset.filter(search)
        return queryset
