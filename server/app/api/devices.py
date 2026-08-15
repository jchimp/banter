"""POST /api/devices/{id}/heartbeat — kidbox liveness/queue-depth ping (M4 step 4).

A thin adapter over `store.upsert_device_heartbeat`: no pre-registration, an
unknown `id` is simply upserted as a new row (same trust model as the
free-form `device_id` on uploads). `last_seen` is always the server's clock,
never client-supplied.
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import store
from app.api.deps import get_settings_dep, require_api_key
from app.config import Settings
from app.db import session, transaction

log = logging.getLogger("banter.api")

router = APIRouter(prefix="/api/devices", tags=["devices"])


class HeartbeatRequest(BaseModel):
    """Body for `POST /api/devices/{id}/heartbeat`."""

    queue_depth: int = Field(ge=0)


@router.post("/{id}/heartbeat", dependencies=[Depends(require_api_key)])
async def device_heartbeat(
    id: str,
    body: HeartbeatRequest,
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    """Upsert a device heartbeat: last-seen time (server clock) and queue depth.

    No 404 and no pre-registration — a device that has never been seen before
    is created on its first heartbeat, same as a kidbox upload's `device_id`.
    """
    last_seen = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    with session(settings.db_path) as conn, transaction(conn):
        store.upsert_device_heartbeat(
            conn,
            device_id=id,
            last_seen=last_seen,
            queue_depth=body.queue_depth,
        )

    log.info(
        "heartbeat_recorded | api | device_id=%s queue_depth=%d", id, body.queue_depth
    )
    return JSONResponse(status_code=200, content={"id": id, "last_seen": last_seen})
