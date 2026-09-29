"""API key scope enforcement (``mcp:leads`` path allowlist)."""

import secrets

import pytest
from fastapi.testclient import TestClient

from app.api_key_scopes import key_allows, unknown_scopes
from app.main import app


@pytest.mark.parametrize(
    "method,path,allowed",
    [
        ("POST", "/api/mcp", True),
        ("GET", "/api/mcp/", True),
        ("GET", "/api/leads", True),
        ("GET", "/api/leads/12", True),
        ("PATCH", "/api/leads/12", True),
        ("DELETE", "/api/leads/12", True),
        ("GET", "/api/leads/12/reply-thread", True),
        ("POST", "/api/campaigns/3/leads", True),
        ("PATCH", "/api/campaigns/3/leads/12", True),
        ("POST", "/api/leads", False),
        ("POST", "/api/leads/bulk-delete", False),
        ("GET", "/api/leads/export", False),
        ("PATCH", "/api/inboxes/1", False),
        ("POST", "/api/inboxes/1/pause", False),
        ("PATCH", "/api/campaigns/3", False),
        ("POST", "/api/auth/api-keys", False),
        ("GET", "/api/settings", False),
    ],
)
def test_mcp_leads_allowlist(method, path, allowed):
    assert key_allows(["mcp:leads"], method, path) is allowed


def test_unscoped_key_has_full_access():
    assert key_allows([], "PATCH", "/api/inboxes/1")
    assert key_allows(None, "POST", "/api/auth/api-keys")


def test_unknown_scope_grants_nothing():
    assert unknown_scopes(["mcp:leads", "admin"]) == ["admin"]
    assert not key_allows(["admin"], "GET", "/api/leads")


def _client():
    # No lifespan: these tests only need routing + auth, not the scheduler/startup jobs.
    return TestClient(app)


async def _add_key(session, scopes):
    """Insert a user + API key hashed with the current in-process secret."""
    from app.auth import hash_api_key
    from app.models import APIKey, User

    user = User(
        username=f"u{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        password_hash="x",
        role="admin",
        is_active=True,
    )
    session.add(user)
    await session.flush()
    raw = f"qk_{secrets.token_urlsafe(24)}"
    session.add(APIKey(user_id=user.id, name="t", key_hash=hash_api_key(raw), prefix=raw[:12], scopes=scopes))
    await session.commit()
    return raw


@pytest.mark.asyncio
async def test_scoped_key_blocked_outside_allowlist(session):
    from tests.conftest import make_inbox

    inbox = await make_inbox(session)
    await session.commit()

    client = _client()
    raw = await _add_key(session, ["mcp:leads"])
    h = {"X-API-Key": raw}

    assert client.get("/api/leads", headers=h).status_code == 200
    assert client.patch(f"/api/inboxes/{inbox.id}", headers=h, json={"max_emails_per_day": 500}).status_code == 403
    assert client.post("/api/auth/api-keys", headers=h, json={"name": "x"}).status_code == 403
    # Bearer form of the same key is checked too
    assert client.get("/api/inboxes", headers={"Authorization": f"Bearer {raw}"}).status_code == 403


@pytest.mark.asyncio
async def test_unscoped_key_still_works(session):
    client = _client()
    raw = await _add_key(session, [])
    r = client.get("/api/inboxes", headers={"X-API-Key": raw})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_create_key_rejects_unknown_scope(session):
    client = _client()
    raw = await _add_key(session, [])
    h = {"X-API-Key": raw}
    assert client.post("/api/auth/api-keys", headers=h, json={"name": "x", "scopes": ["root"]}).status_code == 422
    r = client.post("/api/auth/api-keys", headers=h, json={"name": "agent", "scopes": ["mcp:leads"]})
    assert r.status_code == 200, r.text
