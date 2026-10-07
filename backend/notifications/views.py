from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.views import APIView
from incidents.serializers import StrictSerializer
from evidence.views import PrivateResponseMixin
from common.pagination import StandardPagination
from common.throttling import NotificationRateMixin
from .models import Notification
from .serializers import NotificationSerializer
from .services import mark_read


class InboxView(NotificationRateMixin, PrivateResponseMixin, ListAPIView):
    serializer_class = NotificationSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        from rest_framework.exceptions import ValidationError
        if set(self.request.query_params) - {"page", "page_size"}:
            raise ValidationError("Notification filters are not supported.")
        return Notification.objects.filter(recipient=self.request.user)


class DetailView(NotificationRateMixin, PrivateResponseMixin, RetrieveAPIView):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user)


class UnreadView(NotificationRateMixin, PrivateResponseMixin, APIView):
    def get(self, request):
        return Response({"count": Notification.objects.filter(recipient=request.user, read_at__isnull=True).count()})


class ReadView(NotificationRateMixin, PrivateResponseMixin, APIView):
    def post(self, request, pk=None):
        body = StrictSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response({"updated": mark_read(request.user, pk)})

    patch = post
