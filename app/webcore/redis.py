import redis

from app.core.config import settings

# Dedicated connection pool for WebSocket Pub/Sub, entirely separate from
# app.core.redis_client's pool (used for caching) and from Celery's own
# broker/result-backend connections - all three point at the same Redis
# instance/settings.redis_url, but pub/sub connections are long-lived
# (SUBSCRIBE holds a connection open) and must never be recycled the way a
# short-lived cache GET/SET connection is.
#
# socket_timeout=None is deliberate: BLPOP-style blocking reads (and
# pubsub.get_message with a timeout handled client-side) must not have the
# socket itself time out mid-wait.
_pubsub_pool = redis.ConnectionPool.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=None,
)


def create_pubsub_client() -> redis.Redis:
    """
    Create a dedicated Redis client for WebSocket Pub/Sub, drawn from this
    module's own connection pool.

    The existing app.core.redis_client remains responsible for normal Redis
    operations such as caching, and Celery keeps its own broker/result
    connections - none of those pools are touched or shared here.
    """
    return redis.Redis(connection_pool=_pubsub_pool)


def jd_channel(user_id: str) -> str:
    """
    Channel for the authenticated user's JD upload updates.
    """
    return f"airs:jd:{user_id}"


def campaign_board_channel(campaign_id: str) -> str:
    """
    Channel for candidate-board updates of a campaign.
    """
    return f"airs:board:{campaign_id}"


def resume_processing_channel(task_id: str) -> str:
    """
    Channel for processing updates of a Celery task.
    """
    return f"airs:resume:{task_id}"