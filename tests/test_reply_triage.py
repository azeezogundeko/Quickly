"""Reply triage for external classifiers: reply-thread endpoint, enrollment
change webhooks, and the MCP tools that wrap them."""

import json
from datetime import datetime

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select

from app.models import GmailMessage, GmailThread, LeadReply, QueueSlot, Webhook
from app.routers import campaigns as campaigns_router
from app.routers import leads as leads_router
from app.schemas import CampaignLeadEnrollmentPatch
from tests.conftest import (
    make_campaign,
    make_campaign_lead,
    make_email_log,
    make_inbox,
    make_lead,
    make_queue_slot,
)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


async def _replied_lead(session, *, with_thread: bool = True):
    inbox = await make_inbox(session, email="sender@example.com")
    campaign = await make_campaign(session)
    lead = await make_lead(session, email="prospect@acme.com", name="Ada")
    cl = await make_campaign_lead(session, campaign.id, lead.id)
    log = await make_email_log(session, lead.id, campaign.id, inbox_id=inbox.id)
    session.add(LeadReply(lead_id=lead.id, campaign_id=campaign.id))
    if with_thread:
        log.thread_id = "thr-9"
        session.add(GmailThread(inbox_id=inbox.id, thread_id="thr-9", last_internal_date=_ms(datetime(2026, 2, 1, 10))))
        session.add_all(
            [
                GmailMessage(
                    inbox_id=inbox.id, message_id="m-1", thread_id="thr-9",
                    internal_date=_ms(datetime(2026, 2, 1, 9)), snippet="Hi Ada",
                    headers_json=json.dumps([{"name": "Subject", "value": "Voice agents"}]),
                    label_ids_json=json.dumps(["SENT"]), body_fetched=True, body_plain="Hi Ada, quick question",
                ),
                GmailMessage(
                    inbox_id=inbox.id, message_id="m-2", thread_id="thr-9",
                    internal_date=_ms(datetime(2026, 2, 1, 10)), snippet="Sounds good",
                    headers_json=json.dumps([{"name": "From", "value": "Ada <prospect@acme.com>"}]),
                    label_ids_json=json.dumps(["INBOX"]), body_fetched=True, body_plain="Sounds good, let's talk",
                ),
            ]
        )
    await session.flush()
    return inbox, campaign, lead, cl


# ── GET /api/leads/{id}/reply-thread ──────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_thread_returns_received_body(session):
    _, campaign, lead, _ = await _replied_lead(session)

    data = await leads_router.get_lead_reply_thread(lead.id, campaign_id=campaign.id, db=session)

    assert data["lead_id"] == lead.id
    [thread] = data["threads"]
    assert thread["thread_id"] == "thr-9"
    received = [m for m in thread["messages"] if m["direction"] == "received"]
    assert received[0]["body_plain"] == "Sounds good, let's talk"
    assert "note" not in data


@pytest.mark.asyncio
async def test_reply_thread_unlinked_reply_returns_note(session):
    _, _, lead, _ = await _replied_lead(session, with_thread=False)

    data = await leads_router.get_lead_reply_thread(lead.id, campaign_id=None, db=session)

    assert data["threads"] == []
    assert "Unibox" in data["note"]


@pytest.mark.asyncio
async def test_reply_thread_404_without_reply(session):
    lead = await make_lead(session)
    with pytest.raises(HTTPException) as exc:
        await leads_router.get_lead_reply_thread(lead.id, campaign_id=None, db=session)
    assert exc.value.status_code == 404


# ── PATCH /api/campaigns/{id}/leads/{lead_id} webhooks ────────────────────


@pytest.fixture
def webhook_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_post(webhook, event_type, data):
        calls.append((event_type, data))
        return True

    monkeypatch.setattr("app.webhooks._post_webhook", fake_post)
    return calls


async def _subscribe(session, events):
    session.add(Webhook(url="https://example.com/hook", events=events, active=True, secret=""))
    await session.flush()


async def _patch(session, campaign_id, lead_id, **body):
    return await campaigns_router.patch_campaign_lead(
        campaign_id, lead_id, CampaignLeadEnrollmentPatch(**body), BackgroundTasks(), db=session
    )


@pytest.mark.asyncio
async def test_interest_change_fires_lead_interested(session, webhook_calls):
    _, campaign, lead, _ = await _replied_lead(session)
    await _subscribe(session, ["lead.interested", "lead.status_changed"])

    res = await _patch(session, campaign.id, lead.id, interest="interested")

    assert res["interest"] == "interested"
    assert [e for e, _ in webhook_calls] == ["lead.interested"]
    data = webhook_calls[0][1]
    assert data["lead_email"] == "prospect@acme.com"
    assert data["classification"] == "interested"
    assert data["source"] == "api"


@pytest.mark.asyncio
async def test_same_interest_twice_fires_once(session, webhook_calls):
    _, campaign, lead, _ = await _replied_lead(session)
    await _subscribe(session, ["lead.interested"])

    await _patch(session, campaign.id, lead.id, interest="interested")
    await _patch(session, campaign.id, lead.id, interest="interested")

    assert len(webhook_calls) == 1


@pytest.mark.asyncio
async def test_unsubscribe_fires_events_and_clears_queue(session, webhook_calls):
    inbox, campaign, lead, cl = await _replied_lead(session)
    cl.enrollment_status = "contacted"
    await make_queue_slot(session, cl.id, inbox.id)
    await _subscribe(session, ["lead.unsubscribed", "lead.status_changed"])

    res = await _patch(session, campaign.id, lead.id, status="unsubscribed")

    assert res["status"] == "unsubscribed"
    assert [e for e, _ in webhook_calls] == ["lead.unsubscribed", "lead.status_changed"]
    changed = webhook_calls[1][1]
    assert changed["old_enrollment_status"] == "contacted"
    assert changed["new_enrollment_status"] == "unsubscribed"
    slots = (await session.execute(select(QueueSlot).where(QueueSlot.campaign_lead_id == cl.id))).scalars().all()
    assert slots == []


@pytest.mark.asyncio
async def test_invalid_interest_still_400(session, webhook_calls):
    _, campaign, lead, _ = await _replied_lead(session)
    with pytest.raises(HTTPException) as exc:
        await _patch(session, campaign.id, lead.id, interest="maybe")
    assert exc.value.status_code == 400
    assert webhook_calls == []


# ── MCP tools ─────────────────────────────────────────────────────────────


class _FakeResponse:
    status_code = 200
    is_error = False
    text = "{}"

    def json(self):
        return {"ok": True}


class _FakeClient:
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url, **kw):
        _FakeClient.calls.append(("GET", url, kw))
        return _FakeResponse()

    async def patch(self, url, **kw):
        _FakeClient.calls.append(("PATCH", url, kw))
        return _FakeResponse()


@pytest.fixture
def mcp_http(monkeypatch):
    import app.mcp_leads as mcp_leads

    _FakeClient.calls = []
    monkeypatch.setattr(mcp_leads, "_api_base", lambda: "http://q.test")
    monkeypatch.setattr(mcp_leads, "_outbound_headers", lambda ctx: {"X-API-Key": "k"})
    monkeypatch.setattr(mcp_leads.httpx, "AsyncClient", _FakeClient)
    return _FakeClient.calls


@pytest.mark.asyncio
async def test_mcp_get_reply_thread_calls_endpoint(mcp_http):
    from app.mcp_leads import get_reply_thread

    await get_reply_thread(None, lead_id=7, campaign_id=3)

    [(method, url, kw)] = mcp_http
    assert (method, url) == ("GET", "http://q.test/api/leads/7/reply-thread")
    assert kw["params"] == {"campaign_id": "3"}


@pytest.mark.asyncio
async def test_mcp_set_lead_interest_calls_patch(mcp_http):
    from app.mcp_leads import set_lead_interest

    await set_lead_interest(None, campaign_id=3, lead_id=7, interest="interested")

    [(method, url, kw)] = mcp_http
    assert (method, url) == ("PATCH", "http://q.test/api/campaigns/3/leads/7")
    assert kw["json"] == {"interest": "interested"}


@pytest.mark.asyncio
async def test_mcp_set_lead_interest_requires_a_field(mcp_http):
    from app.mcp_leads import set_lead_interest

    out = json.loads(await set_lead_interest(None, campaign_id=3, lead_id=7))
    assert "error" in out
    assert mcp_http == []
