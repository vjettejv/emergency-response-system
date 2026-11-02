from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsDispatcherOrAdmin, IsRescueTeam
from accounts.permissions import HasRole
from accounts.models import Role
from common.pagination import StandardPagination
from common.throttling import SafeScopedRateThrottle
from .models import ResponseTeam
from .serializers import TeamLocationSerializer, HistoryFilterSerializer, RouteRequestSerializer, StrictSerializer
from .services import update_location
from . import journeys, routing


class GPSUpdateView(APIView):
    permission_classes = [IsRescueTeam]

    def post(self, request):
        return Response(TeamLocationSerializer(update_location(actor=request.user, data=request.data)).data)


class TeamLocationsView(ListAPIView):
    permission_classes = [IsDispatcherOrAdmin]
    serializer_class = TeamLocationSerializer
    pagination_class = StandardPagination
    queryset = ResponseTeam.objects.all().order_by("pk")


class IsTrackingUser(HasRole):
    allowed_roles = (Role.DISPATCHER, Role.ADMIN, Role.RESCUE_TEAM)


class JourneyListView(APIView):
    permission_classes = [IsTrackingUser]

    def get(self, request):
        serializer = HistoryFilterSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        queryset = journeys.journey_list(actor=request.user, values=serializer.validated_data)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(queryset, request)
        response = paginator.get_paginated_response([journeys.summary(item) for item in page])
        response["Cache-Control"] = "no-store"
        return response


class JourneyDetailView(APIView):
    permission_classes = [IsTrackingUser]

    def get(self, request, pk):
        serializer = HistoryFilterSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return Response(journeys.journey_detail(actor=request.user, journey_id=pk, values=serializer.validated_data), headers={"Cache-Control": "no-store"})


class JourneyStopView(APIView):
    permission_classes = [IsRescueTeam]

    def post(self, request):
        serializer = StrictSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        journeys.stop_journey(actor=request.user)
        return Response(status=204)


class RouteView(APIView):
    permission_classes = [IsTrackingUser]
    throttle_classes = [SafeScopedRateThrottle]
    throttle_scope = "routing"

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(routing.route_between(actor=request.user, **serializer.validated_data), headers={"Cache-Control": "no-store"})


class AssignmentRouteView(RouteView):
    def post(self, request, pk):
        serializer = StrictSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(routing.assignment_route(actor=request.user, assignment_id=pk), headers={"Cache-Control": "no-store"})
