import logging
import time
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from . import s3
from .models import MediaAsset

logger = logging.getLogger(__name__)
MAX_CLEANUP_ATTEMPTS = 5


def enqueue_cleanup(media_id, eta=None):
    try:
        cleanup_media.apply_async(args=[str(media_id)], eta=eta, retry=False)
        return True
    except Exception:
        # Durable DB intent is retried by the next sweep after the broker recovers.
        logger.warning("Media cleanup enqueue unavailable media_id=%s", media_id)
        return False


@shared_task(name="evidence.cleanup_media", ignore_result=True)
def cleanup_media(media_id):
    """Idempotent bounded S3 cleanup. Never accept bucket/key from task callers."""
    with transaction.atomic():
        asset = MediaAsset.objects.select_for_update().filter(pk=media_id).first()
        if not asset or asset.status not in ("pending", "deleting") or asset.next_cleanup_at > timezone.now():
            return "skipped"
        # An active image claim may not be deleted before its worker acquires the
        # processing lock. Expired claims are safe to clean and cannot start later.
        if asset.processing_status == "processing" and asset.next_processing_at and asset.next_processing_at > timezone.now():
            return "skipped"
        asset.status = MediaAsset.Status.DELETING
        asset.cleanup_attempts += 1
        logger.info("Media cleanup started media_id=%s attempt=%s", asset.pk, asset.cleanup_attempts)
        try:
            s3.delete_object(asset)
            if asset.optimized_key or asset.processing_attempts:
                s3.delete_optimized(asset)
        except s3.StorageUnavailable:
            asset.cleanup_error = "storage_unavailable"
            asset.status = MediaAsset.Status.FAILED if asset.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS else MediaAsset.Status.DELETING
            asset.next_cleanup_at = timezone.now() + timedelta(seconds=min(900, 30 * 2 ** (asset.cleanup_attempts - 1)))
            logger.warning("Media cleanup failed media_id=%s attempt=%s state=%s", asset.pk, asset.cleanup_attempts, asset.status)
        else:
            asset.status = MediaAsset.Status.DELETED
            asset.cleanup_error = ""
            logger.info("Media cleanup completed media_id=%s", asset.pk)
        asset.save(update_fields=["status", "cleanup_attempts", "cleanup_error", "next_cleanup_at", "updated_at"])
        if asset.status == MediaAsset.Status.DELETING:
            transaction.on_commit(lambda: enqueue_cleanup(str(asset.pk), eta=asset.next_cleanup_at))
        return asset.status


@shared_task(name="evidence.sweep_media", ignore_result=True)
def sweep_media():
    """Recover expired uploads and missed/lost cleanup messages in bounded batches."""
    ids = list(MediaAsset.objects.filter(status__in=["pending", "deleting"], next_cleanup_at__lte=timezone.now())
               .order_by("next_cleanup_at").values_list("pk", flat=True)[:100])
    # Execute within this Celery worker: avoids creating unbounded duplicate tasks.
    processed = 0
    deadline = time.monotonic() + 5
    for media_id in ids:
        if time.monotonic() >= deadline:
            break
        cleanup_media(str(media_id))
        processed += 1
    return processed


MAX_IMAGE_ATTEMPTS = 3
IMAGE_LEASE_SECONDS = 90  # Longer than the existing 60-second hard task limit.


def enqueue_image(media_id, eta=None):
    try:
        optimize_media.apply_async(args=[str(media_id)], eta=eta, retry=False)
        return True
    except Exception:
        logger.warning("Image enqueue unavailable media_id=%s", media_id)
        return False  # The durable due date is recovered by Beat.


@shared_task(name="evidence.optimize_media", ignore_result=True)
def optimize_media(media_id):
    from celery.exceptions import SoftTimeLimitExceeded
    from .images import IMAGE_TYPES, InvalidImage, optimize_image
    # Commit an attempt and a bounded lease before doing I/O. Hard worker loss
    # cannot reset the retry budget or leave processing stuck forever.
    with transaction.atomic():
        asset = MediaAsset.objects.select_for_update().filter(pk=media_id).first()
        if (not asset or asset.status != "ready" or asset.content_type not in IMAGE_TYPES
                or asset.processing_status not in ("pending", "processing")
                or asset.next_processing_at is None or asset.next_processing_at > timezone.now()):
            return "skipped"
        if asset.processing_attempts >= MAX_IMAGE_ATTEMPTS:
            asset.processing_status = "failed"
            asset.processing_error = "attempts_exhausted"
            asset.save(update_fields=["processing_status", "processing_error", "updated_at"])
            return "failed"
        asset.processing_attempts += 1
        attempt = asset.processing_attempts
        asset.processing_status = "processing"
        asset.next_processing_at = timezone.now() + timedelta(seconds=IMAGE_LEASE_SECONDS)
        asset.save(update_fields=["processing_attempts", "processing_status", "next_processing_at", "updated_at"])
    # Serialize processing/cleanup/duplicate messages for this one media row.
    with transaction.atomic():
        asset = MediaAsset.objects.select_for_update().get(pk=media_id)
        if asset.status != "ready" or asset.processing_status != "processing" or asset.processing_attempts != attempt:
            return "skipped"
        started = time.monotonic()
        try:
            data, content_type, width, height = optimize_image(s3.read_original(asset), asset.content_type)
            key, version = s3.store_optimized(asset, data, content_type)
        except (InvalidImage, s3.StorageUnavailable, SoftTimeLimitExceeded) as exc:
            retryable = not isinstance(exc, InvalidImage) and attempt < MAX_IMAGE_ATTEMPTS
            asset.processing_status = "pending" if retryable else "failed"
            asset.processing_error = "invalid_image" if isinstance(exc, InvalidImage) else "processing_unavailable"
            asset.next_processing_at = timezone.now() + timedelta(seconds=min(900, 30 * 2 ** (attempt - 1)))
            asset.save(update_fields=["processing_status", "processing_error", "next_processing_at", "updated_at"])
            logger.warning("Image processing failed media_id=%s attempt=%s state=%s", asset.pk, attempt, asset.processing_status)
            if retryable:
                transaction.on_commit(lambda: enqueue_image(str(asset.pk), eta=asset.next_processing_at))
            return asset.processing_status
        asset.optimized_key, asset.optimized_version_id = key, version
        asset.optimized_size, asset.optimized_content_type = len(data), content_type
        asset.width, asset.height = width, height
        asset.processing_status, asset.processing_error = "ready", ""
        asset.next_processing_at = None
        asset.save(update_fields=["optimized_key", "optimized_version_id", "optimized_size", "optimized_content_type",
                                 "width", "height", "processing_status", "processing_error", "next_processing_at", "updated_at"])
        logger.info("Image processing completed media_id=%s duration_ms=%s", asset.pk, round((time.monotonic() - started) * 1000))
        from realtime.events import publish_after_commit
        from dispatch.models import ACTIVE_ASSIGNMENT_STATUSES
        groups = ["dispatchers"]
        if asset.report_id:
            groups.append(f"user_{asset.report.reporter_id}")
        incident = asset.incident or (asset.report.incident if asset.report_id else None)
        if incident:
            groups += [f"team.{pk}" for pk in incident.assignments.filter(status__in=ACTIVE_ASSIGNMENT_STATUSES).values_list("team_id", flat=True)]
        # Drafts remain private, including processing events.
        if asset.report_id and asset.report.is_draft:
            groups = [f"user_{asset.report.reporter_id}"]
        publish_after_commit(groups, "media.optimized", {"media_id": str(asset.pk)})
        return "ready"


@shared_task(name="evidence.sweep_images", ignore_result=True)
def sweep_images():
    due = MediaAsset.objects.filter(status="ready", processing_status__in=["pending", "processing"],
                                   next_processing_at__lte=timezone.now()).order_by("next_processing_at")
    deadline = time.monotonic() + 5
    processed = 0
    for media_id in list(due.values_list("pk", flat=True)[:20]):
        if time.monotonic() >= deadline:
            break
        # Recover within this worker, as cleanup does, instead of accumulating
        # duplicate queued messages when a processing queue is unavailable.
        optimize_media(str(media_id))
        processed += 1
    return processed
