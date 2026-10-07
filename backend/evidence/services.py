from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from accounts.models import Role
from dispatch.models import ACTIVE_ASSIGNMENT_STATUSES
from incidents.models import Incident, IncidentReport
from incidents.services import Conflict, require_role
from . import s3
from .models import MediaAsset


class StorageError(APIException):
    status_code = 503
    default_detail = "Media storage is unavailable. Retry later."


def parent_for(actor, *, report_id=None, incident_id=None, write=False):
    require_role(actor, (Role.CITIZEN, Role.DISPATCHER, Role.ADMIN, Role.RESCUE_TEAM))
    if (report_id is None) == (incident_id is None):
        raise ValidationError("Specify one media parent.")
    model, pk = (IncidentReport, report_id) if report_id else (Incident, incident_id)
    queryset = model.objects.select_for_update() if write else model.objects.all()
    parent = get_object_or_404(queryset, pk=pk)
    manager = actor.role in (Role.DISPATCHER, Role.ADMIN)
    if report_id:
        rescue_read = not write and actor.role == Role.RESCUE_TEAM and parent.incident_id and actor.response_team_id and parent.incident.status not in ("resolved", "cancelled") and parent.incident.assignments.filter(
            team_id=actor.response_team_id, status__in=ACTIVE_ASSIGNMENT_STATUSES).exists()
        if parent.is_draft and not (actor.role == Role.CITIZEN and parent.reporter_id == actor.pk):
            raise NotFound()
        if not manager and not rescue_read and not (actor.role == Role.CITIZEN and parent.reporter_id == actor.pk):
            raise NotFound()
        if write and not manager and (parent.review_status != "pending" or parent.incident_id):
            raise Conflict("Citizen media changes require an unreviewed, unlinked report.")
    else:
        if not manager and not (actor.role == Role.RESCUE_TEAM and actor.response_team_id and parent.assignments.filter(
            team_id=actor.response_team_id, status__in=ACTIVE_ASSIGNMENT_STATUSES,
        ).exists()):
            raise NotFound()
        if write and parent.status in ("resolved", "cancelled"):
            raise Conflict("Cannot change media on a closed incident.")
    return parent


def media_for(actor, media_id, *, write=False):
    asset = get_object_or_404(MediaAsset, pk=media_id)
    parent_for(actor, report_id=asset.report_id, incident_id=asset.incident_id, write=write)
    if write:
        asset = MediaAsset.objects.select_for_update().get(pk=media_id)
        if actor.role not in (Role.DISPATCHER, Role.ADMIN) and asset.uploaded_by_id != actor.pk:
            raise PermissionDenied("Only the uploader or a dispatcher/admin can modify this media.")
    return asset


@transaction.atomic
def create_upload(*, actor, data):
    from .serializers import UploadSerializer
    serializer = UploadSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    values = dict(serializer.validated_data)
    report_id, incident_id = values.pop("report_id", None), values.pop("incident_id", None)
    parent_for(actor, report_id=report_id, incident_id=incident_id, write=True)
    if actor.role == Role.CITIZEN and values.get("capture_source") != "camera":
        raise ValidationError("Citizen uploads must use the camera flow (client-declared, not proof of location).")
    expires = timezone.now() + timedelta(seconds=settings.MEDIA_UPLOAD_TTL_SECONDS)
    asset = MediaAsset(uploaded_by=actor, report_id=report_id, incident_id=incident_id,
                       bucket=settings.S3_BUCKET_NAME, upload_expires_at=expires,
                       next_cleanup_at=expires + timedelta(seconds=settings.MEDIA_CLEANUP_GRACE_SECONDS), **values)
    target = f"reports/{report_id}" if report_id else f"incidents/{incident_id}"
    asset.object_key = f"evidence/{target}/{asset.pk.hex}"
    try:
        upload = s3.presign_upload(asset)
    except s3.StorageUnavailable as exc:
        raise StorageError from exc
    asset.save()
    return asset, upload


@transaction.atomic
def confirm_upload(*, actor, media_id):
    # An already signed Citizen intent can finish if Dispatcher verifies/links the
    # report during PUT. This does not permit a new upload or deleting reviewed media.
    if actor.role == Role.CITIZEN:
        asset = media_for(actor, media_id)
        if asset.uploaded_by_id != actor.pk:
            raise PermissionDenied("Only the uploader can confirm this media.")
        asset = MediaAsset.objects.select_for_update().get(pk=media_id)
    else:
        asset = media_for(actor, media_id, write=True)
    if asset.status == MediaAsset.Status.READY:
        return asset
    if asset.status != MediaAsset.Status.PENDING or timezone.now() >= asset.upload_expires_at:
        raise Conflict("Upload intent has expired or cannot be confirmed.")
    try:
        info = s3.inspect_object(asset)
        if (info.get("ContentLength") != asset.size_bytes or info.get("ContentType") != asset.content_type
                or info.get("ChecksumSHA256") != asset.checksum_sha256
                or info.get("Metadata", {}).get("media-id") != str(asset.pk)):
            raise ValidationError("Uploaded object does not match the signed metadata/checksum.")
        asset.version_id = info.get("VersionId", "")
        s3.mark_confirmed(asset)
    except s3.ObjectMissing as exc:
        raise Conflict("Uploaded object does not exist yet.") from exc
    except s3.StorageUnavailable as exc:
        raise StorageError from exc
    asset.status = MediaAsset.Status.READY
    asset.confirmed_at = timezone.now()
    from .images import IMAGE_TYPES
    from .tasks import enqueue_image
    if asset.content_type in IMAGE_TYPES:
        asset.processing_status = MediaAsset.ProcessingStatus.PENDING
        asset.next_processing_at = timezone.now()
    asset.save(update_fields=["status", "confirmed_at", "version_id", "updated_at", "processing_status", "next_processing_at"])
    if asset.processing_status == MediaAsset.ProcessingStatus.PENDING:
        transaction.on_commit(lambda: enqueue_image(str(asset.pk)))
    from realtime.events import publish_after_commit
    if not asset.report_id or not IncidentReport.objects.filter(pk=asset.report_id, is_draft=True).exists():
        publish_after_commit(["dispatchers"], "media.confirmed", {"media_id": str(asset.pk), "report_id": asset.report_id, "incident_id": asset.incident_id})
    return asset


@transaction.atomic
def delete_media(*, actor, media_id):
    from .tasks import enqueue_cleanup
    asset = media_for(actor, media_id, write=True)
    if asset.status in (MediaAsset.Status.DELETING, MediaAsset.Status.DELETED, MediaAsset.Status.FAILED):
        return asset
    asset.status = MediaAsset.Status.DELETING
    asset.deleted_by = actor
    asset.next_cleanup_at = max(timezone.now(), asset.upload_expires_at + timedelta(seconds=settings.MEDIA_CLEANUP_GRACE_SECONDS))
    asset.save(update_fields=["status", "deleted_by", "next_cleanup_at", "updated_at"])
    transaction.on_commit(lambda: enqueue_cleanup(str(asset.pk), eta=asset.next_cleanup_at))
    return asset


def download_url(*, actor, media_id, original=False):
    asset = media_for(actor, media_id)
    if asset.status != MediaAsset.Status.READY:
        raise Conflict("Only confirmed media can be downloaded.")
    try:
        optimized = not original and asset.processing_status == "ready" and bool(asset.optimized_key)
        return {"url": s3.presign_optimized(asset) if optimized else s3.presign_download(asset),
                "expires_in": settings.MEDIA_DOWNLOAD_TTL_SECONDS, "variant": "optimized" if optimized else "original"}
    except s3.StorageUnavailable as exc:
        raise StorageError from exc
