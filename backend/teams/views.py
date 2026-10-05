from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsDispatcherOrAdmin, IsRescueTeam
from common.pagination import StandardPagination
from .models import ResponseTeam
from .serializers import TeamLocationSerializer
from .services import update_location


class GPSUpdateView(APIView):
    permission_classes = [IsRescueTeam]

    def post(self, request):
        return Response(TeamLocationSerializer(update_location(actor=request.user, data=request.data)).data)


class TeamLocationsView(ListAPIView):
    permission_classes = [IsDispatcherOrAdmin]
    serializer_class = TeamLocationSerializer
    pagination_class = StandardPagination
    queryset = ResponseTeam.objects.all().order_by("pk")
