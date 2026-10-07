"""Durable, recipient-scoped messages from business services, never reconnects."""
from django.db import transaction
from django.utils import timezone
from accounts.models import Role, User
from realtime.events import publish_after_commit
from .models import Notification
from .serializers import NotificationSerializer


def users(role, **filters):
    return User.objects.filter(is_active=True, role=role, **filters).values_list("pk", flat=True)


@transaction.atomic
def emit(recipients, kind, title, message, event_key, **reference):
    for recipient_id in sorted(set(recipients)):
        notification, created = Notification.objects.get_or_create(
            recipient_id=recipient_id, event_key=event_key,
            defaults={"type": kind, "title": title, "message": message, **reference},
        )
        if created:
            publish_after_commit([f"user_{recipient_id}"], "notification.created", NotificationSerializer(notification).data)


def report_submitted(report):
    emit(users(Role.DISPATCHER), Notification.Type.NEW_REPORT, "Có báo cáo mới",
         "Một báo cáo sự cố đang chờ xác minh.", f"report:{report.pk}:submitted", report_id=report.pk)
    emit(users(Role.CITIZEN, pk=report.reporter_id), Notification.Type.REPORT_RECEIVED, "Đã tiếp nhận báo cáo",
         "Báo cáo của bạn đang chờ điều phối viên xác minh.", f"report:{report.pk}:received", report_id=report.pk)


def report_reviewed(report):
    accepted = report.review_status == "accepted"
    emit(users(Role.CITIZEN, pk=report.reporter_id),
         Notification.Type.REPORT_VERIFIED if accepted else Notification.Type.REPORT_REJECTED,
         "Báo cáo đã được xác minh" if accepted else "Báo cáo chưa được chấp nhận",
         "Điều phối viên đã xác minh báo cáo của bạn." if accepted else "Xem báo cáo để kiểm tra kết quả xác minh.",
         f"report:{report.pk}:review:{report.review_status}", report_id=report.pk)


def report_linked(report, incident_id):
    emit(users(Role.CITIZEN, pk=report.reporter_id), Notification.Type.INCIDENT_CREATED, "Đã ghi nhận sự cố",
         "Báo cáo của bạn đã được liên kết với sự cố để xử lý.",
         f"report:{report.pk}:incident:{incident_id}", report_id=report.pk)


def incident_updated(incident, history_id):
    from dispatch.models import ACTIVE_ASSIGNMENT_STATUSES
    resolved = incident.status == "resolved"
    kind = Notification.Type.INCIDENT_RESOLVED if resolved else Notification.Type.INCIDENT_STATUS_CHANGED
    title = "Sự cố đã được giải quyết" if resolved else "Tiến độ sự cố đã thay đổi"
    reports = incident.reports.filter(is_draft=False).order_by("reporter_id", "pk").distinct("reporter_id")
    for report in reports.only("pk", "reporter_id"):
        emit(users(Role.CITIZEN, pk=report.reporter_id), kind, title, "Xem báo cáo để theo dõi kết quả xử lý.",
             f"incident-history:{history_id}", report_id=report.pk)
    if incident.status in ("verified", "dispatched"):
        return  # Assignment/cancellation already tells the team what to do.
    for assignment in incident.assignments.filter(status__in=ACTIVE_ASSIGNMENT_STATUSES).only("pk", "team_id"):
        emit(users(Role.RESCUE_TEAM, response_team_id=assignment.team_id), kind, "Sự cố nhiệm vụ đã cập nhật",
             "Xem nhiệm vụ để kiểm tra thông tin mới.", f"incident-history:{history_id}", assignment_id=assignment.pk)


def assignment_updated(assignment, history_id):
    team_types = {"pending": (Notification.Type.NEW_ASSIGNMENT, "Bạn có nhiệm vụ mới"),
                  "cancelled": (Notification.Type.ASSIGNMENT_CANCELLED, "Phân công đã được hủy")}
    dispatcher_types = {"accepted": (Notification.Type.ASSIGNMENT_ACCEPTED, "Đội đã nhận nhiệm vụ"),
                        "rejected": (Notification.Type.ASSIGNMENT_REJECTED, "Đội đã từ chối nhiệm vụ"),
                        "on_scene": (Notification.Type.TEAM_ARRIVED, "Đội đã đến hiện trường"),
                        "completed": (Notification.Type.ASSIGNMENT_COMPLETED, "Nhiệm vụ đã hoàn thành")}
    if assignment.status in team_types:
        kind, title = team_types[assignment.status]
        emit(users(Role.RESCUE_TEAM, response_team_id=assignment.team_id), kind, title,
             "Mở nhiệm vụ để xem phân công của điều phối viên.", f"assignment-history:{history_id}", assignment_id=assignment.pk)
    if assignment.status in dispatcher_types:
        kind, title = dispatcher_types[assignment.status]
        emit(users(Role.DISPATCHER, pk=assignment.assigned_by_id), kind, title,
             "Mở sự cố để theo dõi đội ứng cứu.", f"assignment-history:{history_id}", incident_id=assignment.incident_id)


def assistance_requested(signal):
    emit(users(Role.DISPATCHER, pk=signal.assignment.assigned_by_id), Notification.Type.ASSISTANCE_REQUESTED,
         "Đội cần hỗ trợ" if signal.kind == "support" else "Đội báo vấn đề hiện trường",
         "Mở sự cố để xem yêu cầu và quyết định hỗ trợ.", f"assignment-signal:{signal.pk}", incident_id=signal.assignment.incident_id)


@transaction.atomic
def mark_read(actor, notification_id=None):
    queryset = Notification.objects.filter(recipient=actor, read_at__isnull=True)
    if notification_id is not None:
        from django.shortcuts import get_object_or_404
        get_object_or_404(Notification, recipient=actor, pk=notification_id)
        queryset = queryset.filter(pk=notification_id)
    changed = queryset.update(read_at=timezone.now())
    if changed:
        publish_after_commit([f"user_{actor.pk}"], "notification.read", {"id": notification_id, "all": notification_id is None})
    return changed
