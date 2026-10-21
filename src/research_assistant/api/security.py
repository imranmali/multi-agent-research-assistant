"""Static API-key authentication for ``/v1`` routes."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from research_assistant.api.deps import get_app_settings
from research_assistant.config import Settings

__all__ = ["API_KEY_HEADER", "is_valid_api_key", "require_api_key"]

API_KEY_HEADER = "X-API-Key"

_api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="Required when the server is configured with API_KEYS.",
)


def is_valid_api_key(candidate: str | None, keys: list[str]) -> bool:
    """Constant-time comparison against every configured key (no early exit)."""
    if not candidate:
        return False
    supplied = candidate.encode()
    valid = False
    for key in keys:
        valid |= hmac.compare_digest(supplied, key.encode())
    return valid


async def require_api_key(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    api_key: Annotated[str | None, Depends(_api_key_scheme)] = None,
) -> None:
    if not settings.api_keys:
        return
    if not is_valid_api_key(api_key, settings.api_keys):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "APIKey"},
        )
    request.state.authenticated = True
