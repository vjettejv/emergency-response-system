"""Small notifications after commit; REST remains the authoritative snapshot."""

import asyncio
import logging
from uuid import uuid4

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


async def deliver(group, payload):
    await asyncio.wait_for(
        get_channel_layer().group_send(group, {"type": "domain.event", "payload": payload}), timeout=2,
    )


def publish_after_commit(groups, event_type, data):
    payload = {"type": event_type, "event_id": str(uuid4()),
               "emitted_at": timezone.now().isoformat(), "data": data}

    def publish():
        for group in groups:
            try:
                async_to_sync(deliver)(group, payload)
            except Exception:
                # A Redis outage must not make a committed command appear to fail.
                # No token, coordinates or incident details in logs.
                logger.warning("Realtime delivery unavailable for event type %s", event_type)

    transaction.on_commit(publish)


def incident_changed(incident):
    publish_after_commit(["dispatchers"], "incident.status_changed", {
        "incident_id": incident.pk, "status": incident.status, "updated_at": incident.updated_at.isoformat(),
    })


def report_changed(report):
    publish_after_commit(["dispatchers"], "report.changed", {
        "report_id": report.pk, "review_status": report.review_status,
        "incident_id": report.incident_id, "updated_at": report.updated_at.isoformat(),
    })


def assignment_changed(assignment):
    publish_after_commit(["dispatchers", f"team.{assignment.team_id}"], "assignment.status_changed", {
        "assignment_id": assignment.pk, "incident_id": assignment.incident_id,
        "team_id": assignment.team_id,
        "status": "assigned" if assignment.status == "pending" else assignment.status,
        "updated_at": assignment.updated_at.isoformat(), "supersedes": assignment.supersedes_id,
    })
