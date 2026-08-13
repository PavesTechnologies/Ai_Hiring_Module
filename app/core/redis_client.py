import logging

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_pool = redis.ConnectionPool.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)


def get_redis_client() -> redis.Redis:
    """FastAPI dependency yielding a pooled Redis client.

    Connection failures are NOT raised here - the pool is lazy, so an
    unreachable Redis only surfaces as an error on the first command,
    which CacheService catches and treats as a cache miss.
    """
    return redis.Redis(connection_pool=_pool)
