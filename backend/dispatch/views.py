from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsDispatcherOrAdmin, IsRescueTeam
from common.pagination import StandardPagination

from . import services, suggestions
from .models import Assignment
from .serializers import (
    AssignmentHistorySerializer, AssignmentQuerySerializer, AssignmentSerializer,
    AssignTeamSerializer, ExpectedStateSerializer, ReassignSerializer,
    SuggestionsSerializer, TransitionSerializer, SignalSerializer,
)


class SuggestedTeamsView(APIView):
    permission_classes = [IsDispatcherOrAdmin]

    def get(self, request, incident_id):
        result = suggestions.suggest_teams(actor=request.user, incident_id=incident_id)
        return Response(SuggestionsSerializer(result).data)


class AssignTeamView(APIView):
    permission_classes = [IsDispatcherOrAdmin]

    def post(self, request, incident_id):
        serializer = AssignTeamSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment = services.assign_team(actor=request.user, incident_id=incident_id, **serializer.validated_data)
        return Response(AssignmentSerializer(assignment).data, status=status.HTTP_201_CREATED)


class AssignmentViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AssignmentSerializer
    pagination_class = StandardPagination

    def get_permissions(self):
        if self.action in ("cancel", "reassign"):
            return [IsDispatcherOrAdmin()]
        if self.action in ("accept", "change_status") or (self.action == "signals" and self.request.method == "POST") :
            return [IsRescueTeam()]
        return [(IsDispatcherOrAdmin | IsRescueTeam)()]

    def get_queryset(self):
        from .timeline import with_source_times
        queryset = with_source_times(services.assignments_for(self.request.user).select_related("incident")).order_by("-created_at", "-pk")
        if self.action == "list":
            query = AssignmentQuerySerializer(data=self.request.query_params)
            query.is_valid(raise_exception=True)
            queryset = queryset.filter(**query.validated_data)
        return queryset

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        assignment = self.get_object()
        serializer = ExpectedStateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment = services.cancel_assignment(
            actor=request.user, assignment_id=assignment.pk, **serializer.validated_data,
        )
        return Response(AssignmentSerializer(assignment).data)

    @action(detail=True, methods=["post"])
    def reassign(self, request, pk=None):
        assignment = self.get_object()
        serializer = ReassignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        replacement = services.reassign_team(
            actor=request.user, assignment_id=assignment.pk, **serializer.validated_data,
        )
        return Response(AssignmentSerializer(replacement).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        assignment = self.get_object()
        serializer = ExpectedStateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment = services.transition_assignment(
            actor=request.user, assignment_id=assignment.pk, status=Assignment.Status.ACCEPTED,
            **serializer.validated_data,
        )
        return Response(AssignmentSerializer(assignment).data)

    @action(detail=True, methods=["patch"], url_path="status")
    def change_status(self, request, pk=None):
        assignment = self.get_object()
        serializer = TransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment = services.transition_assignment(
            actor=request.user, assignment_id=assignment.pk, **serializer.validated_data,
        )
        return Response(AssignmentSerializer(assignment).data)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        assignment = self.get_object()
        page = self.paginate_queryset(assignment.history.all())
        return self.get_paginated_response(AssignmentHistorySerializer(page, many=True).data)

    @action(detail=True, methods=["get"])
    def contacts(self, request, pk=None):
        from incidents.contacts import contact_for
        from incidents.services import Conflict
        from accounts.models import Role
        from .models import ACTIVE_ASSIGNMENT_STATUSES
        assignment = self.get_object()
        if request.user.role == Role.RESCUE_TEAM and assignment.status not in ACTIVE_ASSIGNMENT_STATUSES:
            raise Conflict("Contact access ends with the assignment.")
        data = [contact_for(request.user, report, request.query_params.get("purpose", ""))
                for report in assignment.incident.reports.order_by("created_at", "pk")]
        return Response(data, headers={"Cache-Control": "no-store"})

    @action(detail=True, methods=["get", "post"])
    def signals(self, request, pk=None):
        assignment = self.get_object()
        if request.method == "POST":
            signal = services.submit_signal(actor=request.user, assignment_id=assignment.pk, data=request.data)
            return Response(SignalSerializer(signal).data, status=status.HTTP_201_CREATED)
        page = self.paginate_queryset(assignment.signals.all())
        return self.get_paginated_response(SignalSerializer(page, many=True).data)
