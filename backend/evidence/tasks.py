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
        asset.status = MediaAsset.Status.DELETING
        asset.cleanup_attempts += 1
        logger.info("Media cleanup started media_id=%s attempt=%s", asset.pk, asset.cleanup_attempts)
        try:
            s3.delete_object(asset)
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
