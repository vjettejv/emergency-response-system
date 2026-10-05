from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import filters, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import IsAdmin
from common.pagination import StandardPagination
from incidents.models import IncidentCategory
from teams.models import ResponseTeam
from . import services
from .serializers import CategorySerializer, ManagedUserSerializer, TeamSerializer


class ManagedViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdmin]
    pagination_class = StandardPagination
    filter_backends = [filters.SearchFilter]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def store(self, serializer):
        raise NotImplementedError

    def persist(self, serializer):
        try:
            obj = self.store(serializer)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
        return Response(self.get_serializer(obj).data, status=status.HTTP_200_OK if serializer.instance else status.HTTP_201_CREATED)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.persist(serializer)

    def partial_update(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return self.persist(serializer)

    def destroy(self, request, *args, **kwargs):
        services.delete_resource(actor=request.user, model=self.queryset.model, object_id=self.get_object().pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserViewSet(ManagedViewSet):
    queryset = User.objects.select_related("response_team").order_by("id")
    serializer_class = ManagedUserSerializer
    search_fields = ["username", "email", "first_name", "last_name"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("role"):
            qs = qs.filter(role=self.request.query_params["role"])
        return qs

    def store(self, serializer):
        return services.save_user(actor=self.request.user, data=dict(serializer.validated_data), user_id=serializer.instance.pk if serializer.instance else None)

    def destroy(self, request, *args, **kwargs):
        services.save_user(actor=request.user, data={"is_active": False}, user_id=self.get_object().pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CategoryViewSet(ManagedViewSet):
    queryset = IncidentCategory.objects.order_by("id")
    serializer_class = CategorySerializer
    search_fields = ["name", "code"]

    def store(self, serializer):
        return services.save_category(actor=self.request.user, data=dict(serializer.validated_data), category_id=serializer.instance.pk if serializer.instance else None)


class TeamViewSet(ManagedViewSet):
    queryset = ResponseTeam.objects.prefetch_related("categories").order_by("id")
    serializer_class = TeamSerializer
    search_fields = ["name", "code"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("status"):
            qs = qs.filter(status=self.request.query_params["status"])
        return qs

    def store(self, serializer):
        return services.save_team(actor=self.request.user, data=dict(serializer.validated_data), team_id=serializer.instance.pk if serializer.instance else None)


class ConfigurationView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response({"read_only": True, "values": {
            name: getattr(settings, name) for name in ["CLUSTER_RADIUS_METERS", "CLUSTER_TIME_WINDOW_MINUTES", "DISPATCH_RADIUS_METERS", "GPS_MIN_INTERVAL_SECONDS", "MEDIA_MAX_BYTES", "MEDIA_UPLOAD_TTL_SECONDS"]
        }})
