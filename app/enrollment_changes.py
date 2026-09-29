"""Apply enrollment status / interest changes and fire the matching webhooks.

The AI classifier (``app/unibox.py``) fires ``lead.{classification}`` when it
labels a reply.  Changes made through the REST API (and so through the MCP
``set_lead_interest`` tool) go through here so an external classifier produces
the same events, plus ``lead.status_changed`` with the real previous status.

Usage::

    events = await apply_enrollment_change(db, cl, status="unsubscribed")
    await db.commit()
    await fire_enrollment_events(db, events)
    await db.commit()  # persist in-app notifications written by the webhooks
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app import time as time_provider
from app.campaign_lead_status import enrollment_blocks_sends
from app.models import CampaignLead, Lead, QueueSlot
from app.webhooks import fire_webhook_event

UNSET: Any = object()

# Statuses that have their own lead.* webhook event (mirrors the classifier).
_STATUS_EVENTS = frozenset({"unsubscribed", "wrong_person"})


async def apply_enrollment_change(
    db: AsyncSession,
    cl: CampaignLead,
    *,
    status: str | None = UNSET,
    interest: str | None = UNSET,
    source: str = "api",
) -> list[tuple[str, dict]]:
    """Set already-validated *status* / *interest* on *cl*; return webhook events to fire.

    Pass ``UNSET`` (the default) to leave a field alone; ``interest=None`` clears it.
    Moving to a status that blocks sends deletes the enrollment's queue slots, as the
    unsubscribe link does, so nothing is left scheduled while the global recalc runs.
    """
    old_status = cl.enrollment_status or "active"
    old_interest = cl.interest_status

    if status is not UNSET and status is not None:
        cl.enrollment_status = status
        if status in _STATUS_EVENTS:
            cl.interest_status = None
    if interest is not UNSET:
        cl.interest_status = interest

    new_status = cl.enrollment_status or "active"
    new_interest = cl.interest_status

    if new_status != old_status and enrollment_blocks_sends(new_status):
        await db.execute(delete(QueueSlot).where(QueueSlot.campaign_lead_id == cl.id))

    lead = await db.get(Lead, cl.lead_id)
    base = {
        "lead_id": cl.lead_id,
        "lead_email": lead.email if lead else "",
        "lead_name": lead.name if lead else "",
        "campaign_id": cl.campaign_id,
        "source": source,
        "timestamp": time_provider.utcnow().isoformat() + "Z",
    }

    events: list[tuple[str, dict]] = []
    if new_interest and new_interest != old_interest:
        events.append((f"lead.{new_interest}", {**base, "classification": new_interest}))
    if new_status != old_status:
        if new_status in _STATUS_EVENTS:
            events.append((f"lead.{new_status}", {**base, "classification": new_status}))
        events.append(
            (
                "lead.status_changed",
                {
                    **base,
                    "old_enrollment_status": old_status,
                    "new_enrollment_status": new_status,
                },
            )
        )
    return events


async def fire_enrollment_events(db: AsyncSession, events: list[tuple[str, dict]]) -> None:
    for event_type, data in events:
        await fire_webhook_event(db, event_type, data)
