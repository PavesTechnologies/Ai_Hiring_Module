import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.middleware.rbac import TokenUser
from app.webcore.redis import campaign_board_channel, jd_channel, resume_processing_channel
from app.websocket.auth import (
    ALL_REALTIME_ROLES,
    RECRUITER_ONLY,
    WebSocketAuthenticationError,
    authenticate_websocket,
    require_websocket_role,
)
from app.websocket.manager import manager
from app.websocket.subscriber import redis_subscriber

logger = logging.getLogger(__name__)

router = APIRouter()


async def _run_websocket_session(
    websocket: WebSocket,
    channel: str,
    log_context: dict,
) -> None:
    """
    Shared lifecycle for every AIRS realtime WebSocket route, once the
    caller has already authenticated and role-checked the connection:

        Register with ConnectionManager
            -> Start the channel's Redis subscriber (idempotent - a no-op
               if another client already opened it)
            -> Keep the socket open until the client disconnects
            -> Always clean up the connection, and stop the Redis
               subscriber once this was the last client on the channel.

    channel is a single Redis Pub/Sub channel name (see app/webcore/redis.py
    for the 3 helpers that build these) - it MUST match exactly what the
    publishing side (app/websocket/publisher.py) writes to, or events are
    silently dropped.
    """

    connected = False
    try:
        await manager.connect(channel, websocket)
        connected = True
        await redis_subscriber.subscribe(channel)

        logger.info("WebSocket connected. channel=%s %s", channel, log_context)

        while True:
            # Realtime updates are server -> client only; any inbound frame
            # is just a keepalive/ping and is otherwise ignored.
            await websocket.receive_text()

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected. channel=%s %s", channel, log_context)

    except Exception:
        logger.exception("Unexpected WebSocket error. channel=%s %s", channel, log_context)
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass

    finally:
        if connected:
            manager.disconnect(channel, websocket)
            if not manager.has_connections(channel):
                await redis_subscriber.unsubscribe(channel)
        logger.info("WebSocket cleanup completed. channel=%s %s", channel, log_context)


async def _authenticate_and_authorize(
    websocket: WebSocket,
    allowed_roles: set[str],
) -> TokenUser | None:
    """
    Runs once per connection, before anything is registered with
    ConnectionManager or the Redis subscriber. Closes the socket with 1008
    and returns None on any authentication/authorization failure; the
    caller must stop (not proceed to _run_websocket_session) when this
    returns None.
    """
    try:
        user = await authenticate_websocket(websocket)
        require_websocket_role(user, allowed_roles)
        return user
    except WebSocketAuthenticationError as exc:
        logger.warning("WebSocket rejected: %s", exc)
        await websocket.close(code=1008, reason=str(exc))
        return None


# ================================================================
# 1. JOB DESCRIPTION UPLOADS
#
# Real-time counterpart of GET /airs/job-descriptions/my-uploads, which is
# already scoped to created_by=the authenticated user - the channel is
# therefore keyed by user_id, not by any job-description/task id.
# ================================================================

@router.websocket("/job-descriptions/my-uploads")
async def job_description_updates(websocket: WebSocket) -> None:
    """Allowed: HR_ADMIN, RECRUITER, HIRING_MANAGER."""
    user = await _authenticate_and_authorize(websocket, ALL_REALTIME_ROLES)
    if user is None:
        return

    channel = jd_channel(user.user_id)
    await _run_websocket_session(websocket, channel, {"user_id": user.user_id})


# ================================================================
# 2. CAMPAIGN CANDIDATE BOARD
# ================================================================

@router.websocket("/campaign-candidates/campaign/{campaign_id}/board")
async def campaign_board_updates(websocket: WebSocket, campaign_id: UUID) -> None:
    """
    Allowed: HR_ADMIN, RECRUITER, HIRING_MANAGER.

    Mirrors GET /airs/campaign-candidates/campaign/{campaign_id}/board's own
    authorization exactly: role-gated only. That HTTP endpoint does not
    check campaign ownership/assignment (HiringCampaign.hiring_manager_id/
    recruiter_id are not enforced anywhere in the existing code - see
    CampaignRepository.get_by_id / CampaignCandidateService.get_campaign_board),
    so no additional campaign-access check is introduced here either.
    """
    user = await _authenticate_and_authorize(websocket, ALL_REALTIME_ROLES)
    if user is None:
        return

    channel = campaign_board_channel(str(campaign_id))
    await _run_websocket_session(
        websocket, channel, {"user_id": user.user_id, "campaign_id": str(campaign_id)},
    )


# ================================================================
# 3. RESUME PROCESSING STATUS
#
# IMPORTANT: the path parameter is task_id (the Celery task_id), exactly
# like GET /airs/resumes/processing-status/{task_id} - never a resume_id.
# ================================================================

@router.websocket("/resumes/processing-status/{task_id}")
async def resume_processing_updates(websocket: WebSocket, task_id: str) -> None:
    """Allowed: RECRUITER only (narrower than the HTTP endpoint, which also allows HR_ADMIN)."""
    user = await _authenticate_and_authorize(websocket, RECRUITER_ONLY)
    if user is None:
        return

    channel = resume_processing_channel(task_id)
    await _run_websocket_session(
        websocket, channel, {"user_id": user.user_id, "task_id": task_id},
    )
