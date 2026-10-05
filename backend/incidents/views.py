from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models.functions import Coalesce

from accounts.models import Role
from accounts.permissions import IsCitizen, IsDispatcherOrAdmin, IsRescueTeam
from common.pagination import StandardPagination as IncidentPagination

from . import clustering, services, contacts
from dispatch.models import AssignmentSignal
from .filters import IncidentDataFilter
from .models import Incident, IncidentCategory, IncidentReport
from .serializers import (
    CategorySerializer,
    DismissDuplicateSerializer,
    DuplicateDismissalSerializer,
    HistorySerializer,
    IncidentFromReportSerializer,
    IncidentSerializer,
    IncidentStatusSerializer,
    LinkReportsSerializer,
    PotentialDuplicatesSerializer,
    ReportCreateSerializer,
    ReportReviewSerializer,
    ReportLinkHistorySerializer,
    ReportSerializer,
    StrictSerializer,
    DraftReportSerializer,
)


class CategoryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = IncidentCategory.objects.filter(is_active=True).order_by("name", "pk")
    serializer_class = CategorySerializer
    pagination_class = IncidentPagination


class ReportViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet,
):
    serializer_class = ReportSerializer
    pagination_class = IncidentPagination
    filter_backends = [IncidentDataFilter]

    def get_permissions(self):
        if self.action in ("retrieve", "contact"):
            return [(IsCitizen | IsDispatcherOrAdmin | IsRescueTeam)()]
        if self.action in ("create", "drafts", "submit"):
            return [IsCitizen()]
        if self.action in ("verify", "create_incident", "potential_duplicates", "dismiss_duplicate"):
            return [IsDispatcherOrAdmin()]
        return [(IsCitizen | IsDispatcherOrAdmin)()]

    def get_queryset(self):
        queryset = contacts.reports_for(self.request.user).order_by(Coalesce("submitted_at", "created_at").desc(), "-pk")
        if self.request.user.role == Role.CITIZEN:
            queryset = queryset.filter(reporter=self.request.user)
        return queryset

    def filter_queryset(self, queryset):
        # Collection filters must not affect mutation/detail lookups.
        return super().filter_queryset(queryset) if self.action == "list" else queryset

    def create(self, request, *args, **kwargs):
        serializer = ReportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = services.submit_report(actor=request.user, **serializer.validated_data)
        data = ReportSerializer(report).data
        data["potential_duplicates"] = report.potential_duplicate_hint
        return Response(data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def drafts(self, request):
        serializer = DraftReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        request_id = values.pop("request_id")
        report = services.save_report_draft(actor=request.user, request_id=request_id, values=values)
        return Response(ReportSerializer(report).data, status=201, headers={"Cache-Control": "no-store"})

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        body = StrictSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        report = services.finalize_report(actor=request.user, report_id=self.get_object().pk)
        data = ReportSerializer(report).data
        if hasattr(report, "potential_duplicate_hint"):
            data["potential_duplicates"] = report.potential_duplicate_hint
        return Response(data)

    @action(detail=True, methods=["get"])
    def contact(self, request, pk=None):
        return Response(contacts.contact_for(request.user, self.get_object(), request.query_params.get("purpose", "")),
                        headers={"Cache-Control": "no-store"})

    @action(detail=True, methods=["get"], url_path="potential-duplicates")
    def potential_duplicates(self, request, pk=None):
        report = self.get_object()
        result = clustering.find_potential_duplicates(actor=request.user, report_id=report.pk)
        return Response(PotentialDuplicatesSerializer(result).data)

    @action(detail=True, methods=["post"], url_path="dismiss-duplicate")
    def dismiss_duplicate(self, request, pk=None):
        report = self.get_object()
        serializer = DismissDuplicateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dismissal = clustering.dismiss_potential_duplicate(
            actor=request.user, report_id=report.pk, **serializer.validated_data,
        )
        return Response(DuplicateDismissalSerializer(dismissal).data)

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        report = self.get_object()
        serializer = ReportReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = services.review_report(actor=request.user, report_id=report.pk, **serializer.validated_data)
        return Response(ReportSerializer(report).data)

    @action(detail=True, methods=["post"], url_path="create-incident")
    def create_incident(self, request, pk=None):
        report = self.get_object()
        serializer = IncidentFromReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        incident = services.create_incident_from_report(
            actor=request.user, report_id=report.pk, **serializer.validated_data,
        )
        return Response(IncidentSerializer(incident).data, status=status.HTTP_201_CREATED)


class IncidentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Incident.objects.all().order_by("-created_at", "-pk")
    serializer_class = IncidentSerializer
    permission_classes = [IsDispatcherOrAdmin]
    pagination_class = IncidentPagination
    filter_backends = [IncidentDataFilter]

    def filter_queryset(self, queryset):
        return super().filter_queryset(queryset) if self.action == "list" else queryset

    @action(detail=False, methods=["get"])
    def metrics(self, request):
        from dispatch.timeline import response_metrics
        return Response(response_metrics())

    @action(detail=True, methods=["get"])
    def contacts(self, request, pk=None):
        return Response(contacts.incident_contacts(request.user, self.get_object().pk, request.query_params.get("purpose", "")),
                        headers={"Cache-Control": "no-store"})

    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        from dispatch.timeline import incident_timeline
        return Response(incident_timeline(self.get_object()))

    @action(detail=True, methods=["get"])
    def signals(self, request, pk=None):
        from dispatch.serializers import SignalSerializer
        incident = self.get_object()
        page = self.paginate_queryset(AssignmentSignal.objects.filter(assignment__incident=incident))
        return self.get_paginated_response(SignalSerializer(page, many=True).data)

    @action(detail=True, methods=["get", "post"])
    def reports(self, request, pk=None):
        incident = self.get_object()
        if request.method == "POST":
            serializer = LinkReportsSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            incident = services.link_reports(
                actor=request.user, incident_id=incident.pk, **serializer.validated_data,
            )
            return Response(IncidentSerializer(incident).data)
        queryset = incident.reports.order_by("-created_at", "-pk")
        queryset = IncidentDataFilter().filter_queryset(request, queryset, self)
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(ReportSerializer(page, many=True).data)

    @action(detail=True, methods=["patch"], url_path="status")
    def change_status(self, request, pk=None):
        incident = self.get_object()
        serializer = IncidentStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        incident = services.change_incident_status(
            actor=request.user, incident_id=incident.pk, **serializer.validated_data,
        )
        return Response(IncidentSerializer(incident).data)

    @action(detail=True, methods=["get"], url_path="status-history")
    def status_history(self, request, pk=None):
        incident = self.get_object()
        page = self.paginate_queryset(incident.status_history.all())
        return self.get_paginated_response(HistorySerializer(page, many=True).data)

    @action(detail=True, methods=["get"], url_path="report-link-history")
    def report_link_history(self, request, pk=None):
        incident = self.get_object()
        page = self.paginate_queryset(incident.report_link_history.all())
        return self.get_paginated_response(ReportLinkHistorySerializer(page, many=True).data)
