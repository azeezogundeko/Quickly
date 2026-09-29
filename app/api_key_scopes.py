"""API key scope enforcement.

A key with no scopes keeps full access (the owner's permissions), so keys
created before scopes were enforced keep working.  A scoped key may only
reach the endpoints its scopes allow; everything else is denied, including
``/api/auth/*`` so a scoped key cannot mint an unscoped one.

``mcp:leads`` covers the MCP endpoint plus every REST route the MCP lead
tools call back into (see ``app/mcp_leads.py``).
"""
from __future__ import annotations

import re
from typing import Iterable

_ANY = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})

# scope -> list of (allowed methods, compiled path regex)
_SCOPE_RULES: dict[str, list[tuple[frozenset[str], re.Pattern[str]]]] = {
    "mcp:leads": [
        (_ANY, re.compile(r"^/api/mcp(/.*)?$")),
        (frozenset({"GET"}), re.compile(r"^/api/leads/?$")),
        (frozenset({"GET", "PATCH", "DELETE"}), re.compile(r"^/api/leads/\d+/?$")),
        (frozenset({"GET"}), re.compile(r"^/api/leads/\d+/reply-thread/?$")),
        (frozenset({"POST"}), re.compile(r"^/api/campaigns/\d+/leads/?$")),
        (frozenset({"PATCH"}), re.compile(r"^/api/campaigns/\d+/leads/\d+/?$")),
    ],
}

KNOWN_SCOPES = frozenset(_SCOPE_RULES)


def unknown_scopes(scopes: Iterable[str]) -> list[str]:
    """Return the scope strings that are not recognised."""
    return sorted({s for s in scopes if s not in KNOWN_SCOPES})


def key_allows(scopes: Iterable[str] | None, method: str, path: str) -> bool:
    """True if a key with *scopes* may call ``method path``."""
    scopes = [s for s in (scopes or []) if s]
    if not scopes:
        return True
    method = method.upper()
    for scope in scopes:
        for methods, pattern in _SCOPE_RULES.get(scope, ()):
            if method in methods and pattern.match(path):
                return True
    return False
