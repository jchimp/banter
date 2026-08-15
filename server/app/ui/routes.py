"""Web UI routes (M4): recording list, source filter, browser audio playback.

No auth on anything here (FR-25, trusted-LAN). This is precisely why the audio
route exists separately from `/api/recordings/{id}/audio` — a plain `<audio src>`
tag can't send `X-API-Key`, so this module exposes an unauthenticated sibling that
still goes through `app.audio.resolve_playable_audio`, the single owner of the
path-escape guard.

Soft-delete/undo (FR-23) and the read-only device panel (FR-24) are later steps.
This module only builds the list + filter + playback foundation they'll sit on top
of; `index.html` leaves a marked placeholder for the device panel.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app import store
from app.api.deps import get_settings_dep
from app.audio import parse_iso8601, resolve_playable_audio
from app.config import Settings
from app.db import session

log = logging.getLogger("banter.ui")

router = APIRouter(tags=["ui"])

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Sources the filter bar/route accept. `store.list_recordings` deliberately does not
# validate `source` itself (it's a plain data-access function); this route is the
# right layer to do it, since it also owns the "what does an invalid value mean"
# UX decision below.
_VALID_SOURCES = {"kid", "mom", "dad"}


def _normalize_source(source: str | None) -> str | None:
    """Validate the `?source=` query param for list routes.

    An unrecognized value (typo'd query string, stale bookmark, hand-edited URL)
    is treated as "no filter" rather than a 422. This is a family utility page on
    the LAN, not a public API: a bad filter value degrading to "show everything"
    is a better experience than an error page, and it keeps the filter bar's own
    links (which only ever emit known values) indistinguishable in behavior from a
    slightly-mistyped one.
    """
    if source in _VALID_SOURCES:
        return source
    return None


def _format_duration(duration_ms: int | None) -> str:
    """`Ns` under a minute, `M:SS` at or above.

    Mirrors `app.telegram.notify._format_duration` visually (CLAUDE.md: stay
    consistent rather than inventing a third format). Not imported from there
    because it's a private, module-local helper in `notify.py` and this module has
    no other reason to depend on `app.telegram`; duplicating four lines is cheaper
    than crossing that boundary for one function.
    """
    if not duration_ms or duration_ms < 0:
        return "0s"
    total_seconds = round(duration_ms / 1000)
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _format_timestamp(created_at: str) -> str:
    """Readable UTC timestamp, e.g. `Aug 12, 10:30 UTC`.

    Mirrors the caption timestamp format in `app.telegram.notify.caption_for` (same
    duplication rationale as `_format_duration` above: that function is private to
    `notify.py`, so this reproduces the format rather than importing across the
    module boundary).
    """
    dt = parse_iso8601(created_at)
    return f"{dt:%b} {dt.day}, {dt:%H:%M} UTC"


def _origin_label(origin: str) -> str:
    """Short origin label for the source badge (FR-21): `kidbox` or `tg`."""
    return "kidbox" if origin == "kidbox" else "tg"


templates.env.filters["duration"] = _format_duration
templates.env.filters["friendly_time"] = _format_timestamp
templates.env.filters["origin_label"] = _origin_label


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    source: str | None = None,
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    """Full recordings page. Accepts `?source=` so a pushed URL loads the same view.

    Args:
        request: Current request, required by `Jinja2Templates` for URL generation.
        source: Optional source filter (`kid`, `mom`, `dad`); anything else is
            treated as no filter, see `_normalize_source`.
        settings: App settings, injected.

    Returns:
        The full HTML page.
    """
    normalized = _normalize_source(source)
    with session(settings.db_path) as conn:
        recordings = store.list_recordings(conn, source=normalized)

    return templates.TemplateResponse(
        request,
        "index.html",
        {"recordings": recordings, "source": normalized},
    )


@router.get("/ui/recordings", response_class=HTMLResponse)
def recordings_partial(
    request: Request,
    source: str | None = None,
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    """HTMX partial: the recordings list fragment only, no `<html>`/`<head>`.

    Same `?source=` handling as `index()` so an `hx-get` swap and a fresh
    `hx-push-url` reload of the same URL render identical markup.

    Args:
        request: Current request, required by `Jinja2Templates`.
        source: Optional source filter; see `_normalize_source`.
        settings: App settings, injected.

    Returns:
        The bare `_recordings_list.html` fragment.
    """
    normalized = _normalize_source(source)
    with session(settings.db_path) as conn:
        recordings = store.list_recordings(conn, source=normalized)

    return templates.TemplateResponse(
        request,
        "_recordings_list.html",
        {"recordings": recordings, "source": normalized},
    )


@router.get("/ui/recordings/{id}/audio")
def recording_audio(
    id: str,
    settings: Settings = Depends(get_settings_dep),
) -> FileResponse:
    """Stream a recording's WAV file to the browser `<audio>` tag. No auth (FR-25).

    All path-escape/soft-delete/missing-file guarding lives in
    `app.audio.resolve_playable_audio` — the single owner shared with the
    auth-gated `/api/recordings/{id}/audio` route. This handler is just the
    unauthenticated adapter a browser `<audio src>` can actually hit.

    Args:
        id: Recording id.
        settings: App settings, injected.

    Returns:
        The WAV file.
    """
    with session(settings.db_path) as conn:
        resolved = resolve_playable_audio(conn, settings, id)

    return FileResponse(resolved, media_type="audio/wav")
