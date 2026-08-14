"""FastAPI dependencies shared across `/api/*` routes."""

import logging
import secrets

from fastapi import Header, HTTPException, Request

from app.config import Settings

log = logging.getLogger("banter.api")

_auth_disabled_warned = False


def get_settings_dep(request: Request) -> Settings:
    """Settings live on `app.state`, not `get_settings()` — tests inject a tmp-dir Settings."""
    return request.app.state.settings


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Check `X-API-Key` against `settings.api_key` using a constant-time compare.

    An empty `api_key` disables auth entirely (dev only); this is logged once, not
    on every request.
    """
    settings: Settings = request.app.state.settings

    if settings.api_key == "":
        global _auth_disabled_warned
        if not _auth_disabled_warned:
            log.warning("auth_disabled reason=empty_api_key")
            _auth_disabled_warned = True
        return

    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
