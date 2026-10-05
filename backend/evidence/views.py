from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView
from common.pagination import StandardPagination
from incidents.serializers import StrictSerializer
from . import services
from django.db.models import Q
from .models import MediaAsset
from .serializers import MediaSerializer, ParentSerializer


class PrivateResponseMixin:
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class PresignView(PrivateResponseMixin, APIView):
    def post(self, request):
        asset, upload = services.create_upload(actor=request.user, data=request.data)
        return Response({"media": MediaSerializer(asset).data, "upload": upload}, status=status.HTTP_201_CREATED)


class ConfirmView(PrivateResponseMixin, APIView):
    def post(self, request, media_id):
        body = StrictSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(MediaSerializer(services.confirm_upload(actor=request.user, media_id=media_id)).data)


class MediaListView(PrivateResponseMixin, ListAPIView):
    serializer_class = MediaSerializer
    pagination_class = StandardPagination

    def get_queryset(self):
        query = ParentSerializer(data={k: v for k, v in self.request.query_params.items() if k not in ("page", "page_size")})
        query.is_valid(raise_exception=True)
        services.parent_for(self.request.user, **query.validated_data)
        if "incident_id" in query.validated_data:
            incident_id = query.validated_data["incident_id"]
            return MediaAsset.objects.filter(Q(incident_id=incident_id) | Q(report__incident_id=incident_id)).exclude(status="deleted")
        return MediaAsset.objects.filter(**query.validated_data).exclude(status="deleted")


class MediaDetailView(PrivateResponseMixin, APIView):
    def get(self, request, media_id):
        return Response(MediaSerializer(services.media_for(request.user, media_id)).data)

    def delete(self, request, media_id):
        return Response(MediaSerializer(services.delete_media(actor=request.user, media_id=media_id)).data, status=202)


class DownloadView(PrivateResponseMixin, APIView):
    def get(self, request, media_id):
        return Response(services.download_url(actor=request.user, media_id=media_id))
