"""POST /api/recordings — multipart upload from kidbox (PRD §5).

Idempotent on the client-generated `id`: a retried upload with the same id is
detected before the body is even read, so retries are cheap and never rewrite a
stored file (CLAUDE.md: "the client's queue is the source of truth for unsent
audio... client generates the id; server upserts on it").
"""

import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app import store
from app.api.deps import get_settings_dep, require_api_key
from app.audio import (
    InvalidAudioError,
    audio_path,
    incoming_path,
    is_canonical,
    parse_iso8601,
    probe_wav,
)
from app.config import Settings
from app.db import session, transaction

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
    id: str = Form(...),
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
