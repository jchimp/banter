"""POST /api/recordings — multipart upload from kidbox (PRD §5).

Also the M2 playback routes: `GET /next` (selection), `GET /{id}/audio` (stream),
`POST /{id}/played` (receipt). Those three are thin adapters over `app.selection`
and `app.store` — no tier/cooldown logic here (FR-14: it lives in one place).

Idempotent on the client-generated `id`: a retried upload with the same id is
detected before the body is even read, so retries are cheap and never rewrite a
stored file (CLAUDE.md: "the client's queue is the source of truth for unsent
audio... client generates the id; server upserts on it").
"""

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from app import store
from app.api.deps import get_settings_dep, require_api_key
from app.audio import (
    InvalidAudioError,
    audio_path,
    incoming_path,
    is_canonical,
    parse_iso8601,
    probe_wav,
    resolve_playable_audio,
)
from app.config import Settings
from app.db import session, transaction
from app.selection import DeviceHistory, SelectionConfig, select_next

log = logging.getLogger("banter.api")

router = APIRouter(prefix="/api", tags=["recordings"])

_VALID_SOURCES = {"kid", "mom", "dad"}
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,64}$")
_CHUNK_SIZE = 64 * 1024


async def _stream_to_file(upload: UploadFile, dest: Path, max_bytes: int) -> None:
    """Stream `upload` into `dest` in chunks, aborting once `max_bytes` is exceeded.

    Never buffers the whole upload in memory. The caller is responsible for removing
    a partial `dest` on failure.

    Raises:
        HTTPException: 413 if the running total exceeds `max_bytes`.
    """
    total = 0
    with dest.open("wb") as f:
        while chunk := await upload.read(_CHUNK_SIZE):
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(status_code=413, detail="upload exceeds max_upload_bytes")
            f.write(chunk)


@router.post("/recordings", dependencies=[Depends(require_api_key)])
async def upload_recording(
    # `default=""` (not `Form(...)`) so a missing/empty `id` reaches the handler
    # instead of short-circuiting with FastAPI's 422 — the kidbox shouldn't have
    # to distinguish "missing" from "malformed", both are just a 400 here.
    id: str = Form(default=""),
    audio: UploadFile = File(...),
    source: str = Form(...),
    device_id: str = Form(...),
    recorded_at: str = Form(...),
    duration_ms: int = Form(...),
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    """Accept a kidbox recording upload; probe the file server-side, store it, upsert the row.

    `duration_ms` is the client's estimate for logging/telemetry only — the row's
    stored `duration_ms`/`bytes` always come from the server-side WAV probe.
    `device_id` is accepted and logged but has no table write in M1; the `devices`
    heartbeat lands in M4.
    """
    if source not in _VALID_SOURCES:
        raise HTTPException(status_code=400, detail=f"invalid source: {source!r}")
    if not _ID_RE.match(id):
        raise HTTPException(status_code=400, detail="invalid id")

    try:
        recorded_dt = parse_iso8601(recorded_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid recorded_at: {exc}") from exc

    log.info(
        "upload_received | api | id=%s source=%s device_id=%s client_duration_ms=%d",
        id,
        source,
        device_id,
        duration_ms,
    )

    with session(settings.db_path) as conn:
        # Short-circuit on a known id: no re-probe, no rewrite of a stored file. Note
        # the body has already been spooled by FastAPI's form parsing at this point,
        # so `max_upload_bytes` bounds what we *keep*, not what we receive. That is
        # fine for a key-gated box on the LAN; a real ingress cap would be middleware.
        if store.get_recording(conn, id) is not None:
            log.info("upload_duplicate | api | id=%s", id)
            return JSONResponse(status_code=200, content={"id": id, "status": "duplicate"})

        staged = incoming_path(settings.audio_dir, id)
        staged.parent.mkdir(parents=True, exist_ok=True)
        try:
            await _stream_to_file(audio, staged, settings.max_upload_bytes)

            try:
                info = probe_wav(staged)
            except InvalidAudioError as exc:
                raise HTTPException(status_code=400, detail=f"invalid audio: {exc}") from exc

            if not is_canonical(info):
                log.warning(
                    "offspec_wav id=%s rate=%d channels=%d",
                    id,
                    info.sample_rate,
                    info.channels,
                )

            dest = audio_path(settings.audio_dir, source, recorded_dt, id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, dest)
            rel_path = dest.relative_to(settings.audio_dir).as_posix()

            created_at = recorded_dt.isoformat().replace("+00:00", "Z")
            with transaction(conn):
                created = store.insert_recording(
                    conn,
                    id=id,
                    source=source,
                    origin="kidbox",
                    path=rel_path,
                    duration_ms=info.duration_ms,
                    bytes=info.bytes,
                    created_at=created_at,
                )
        finally:
            # Only ever removes the staging file — never the canonical audio/ tree.
            staged.unlink(missing_ok=True)

    if created:
        log.info("upload_created | api | id=%s path=%s", id, rel_path)
        return JSONResponse(status_code=201, content={"id": id, "status": "created"})
    # Lost a race with a concurrent identical upload: file is already in place, leave it.
    log.info("upload_race_duplicate | api | id=%s", id)
    return JSONResponse(status_code=200, content={"id": id, "status": "duplicate"})


@router.get("/recordings/next", dependencies=[Depends(require_api_key)])
async def next_recording(
    device_id: str = Query(...),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    """Pick the next recording for `device_id` to play (PRD 3.3, FR-14).

    A thin adapter: build `Candidate`/`DeviceHistory` from the store, hand them to
    the pure `select_next`, map the result back to JSON. No tier reasoning or
    cooldown arithmetic lives here — that's the whole point of `app.selection`.
    """
    with session(settings.db_path) as conn:
        candidates = store.selectable_candidates(conn, device_id)
        history = DeviceHistory(last_recording_id=store.last_played_recording_id(conn, device_id))

        cfg = SelectionConfig(
            parent_cooldown_hours=settings.parent_cooldown_hours,
            avoid_immediate_repeat=settings.avoid_immediate_repeat,
        )
        chosen = select_next(candidates, history, datetime.now(UTC), cfg)

        if chosen is None:
            log.info("next_empty | api | device_id=%s", device_id)
            return Response(status_code=204)

        # `Candidate` carries only what selection needs; `duration_ms` isn't part of
        # that pure-function shape, so pull the full row for the response payload.
        row = store.get_recording(conn, chosen.id)

    log.info(
        "next_chosen | api | device_id=%s id=%s source=%s", device_id, chosen.id, chosen.source
    )
    return JSONResponse(
        status_code=200,
        content={
            "id": chosen.id,
            "source": chosen.source,
            "duration_ms": row["duration_ms"],
            "created_at": chosen.created_at.isoformat().replace("+00:00", "Z"),
            "play_count": chosen.play_count,
            "audio_url": f"/api/recordings/{chosen.id}/audio",
        },
    )


@router.get("/recordings/{id}/audio", dependencies=[Depends(require_api_key)])
async def get_recording_audio(
    id: str,
    settings: Settings = Depends(get_settings_dep),
) -> FileResponse:
    """Stream a recording's WAV file.

    404 on any failure mode — unknown/soft-deleted id, path escape, or missing file
    on disk. All of that lives in `app.audio.resolve_playable_audio`, the single
    owner of the path-escape guard shared with the (unauthenticated) web-UI playback
    route; this handler is just the auth-gated adapter over it.
    """
    with session(settings.db_path) as conn:
        resolved = resolve_playable_audio(conn, settings, id)

    return FileResponse(resolved, media_type="audio/wav")


class PlayedRequest(BaseModel):
    """Body for `POST /api/recordings/{id}/played`."""

    device_id: str
    played_at: str | None = None


@router.post("/recordings/{id}/played", dependencies=[Depends(require_api_key)])
async def mark_played(
    id: str,
    body: PlayedRequest,
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    """Record a play receipt for `id` on `body.device_id`.

    404 only when `id` is genuinely unknown — a soft-deleted recording still
    records the play (it may have been deleted after the kid heard it, per the
    task spec). `played_at` defaults to now server-side; a caller-supplied value
    is validated as ISO8601 (400 on garbage) so a retried receipt can resend the
    exact same timestamp and be deduped by `store.record_play`.
    """
    if body.played_at is not None:
        try:
            parse_iso8601(body.played_at)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid played_at: {exc}") from exc
        played_at = body.played_at
    else:
        played_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    with session(settings.db_path) as conn:
        existing = store.get_recording(conn, id)
        if existing is None:
            raise HTTPException(status_code=404, detail="recording not found")

        with transaction(conn):
            store.record_play(
                conn,
                recording_id=id,
                device_id=body.device_id,
                played_at=played_at,
            )
        row = store.get_recording(conn, id)

    log.info(
        "played_recorded | api | id=%s device_id=%s played_at=%s", id, body.device_id, played_at
    )
    return JSONResponse(status_code=200, content={"id": id, "play_count": row["play_count"]})
