import redis
from fastapi import Depends

from app.core.redis_client import get_redis_client
from app.services.cache_service import CacheService


def get_cache_service(
    client: redis.Redis = Depends(get_redis_client),
) -> CacheService:
    return CacheService(client)
