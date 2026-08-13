import logging
import time
from typing import Any, Callable, Optional

import redis

from app.core.cache_keys import lock_key
from app.core.config import settings

logger = logging.getLogger(__name__)


class CacheService:
    """Cache-aside helper over Redis.

    Every Redis call is guarded: if Redis is unreachable or errors, methods
    log a warning and behave as a cache miss / no-op so callers always fall
    back to PostgreSQL instead of failing the request.
    """

    def __init__(self, client: redis.Redis):
        self._client = client

    def get(self, key: str) -> Optional[str]:
        try:
            return self._client.get(key)
        except redis.exceptions.RedisError:
            logger.warning("Redis GET failed for key=%s, treating as cache miss", key, exc_info=True)
            return None

    def set(self, key: str, value: str, ttl: Optional[int] = None) -> bool:
        try:
            self._client.set(key, value, ex=ttl or settings.cache_default_ttl_seconds)
            return True
        except redis.exceptions.RedisError:
            logger.warning("Redis SET failed for key=%s", key, exc_info=True)
            return False

    def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            self._client.delete(*keys)
        except redis.exceptions.RedisError:
            logger.warning("Redis DELETE failed for keys=%s", keys, exc_info=True)

    def delete_by_prefix(self, prefix: str) -> None:
        try:
            cursor = 0
            while True:
                cursor, keys = self._client.scan(cursor=cursor, match=f"{prefix}*", count=200)
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
        except redis.exceptions.RedisError:
            logger.warning("Redis SCAN/DELETE by prefix failed for prefix=%s", prefix, exc_info=True)

    def get_or_set(
        self,
        key: str,
        loader: Callable[[], str],
        ttl: Optional[int] = None,
        use_lock: bool = True,
    ) -> str:
        """Cache-aside with lightweight stampede protection.

        `loader` must return the already-serialized (JSON string) value to
        cache. Returns the cached or freshly loaded value either way -
        callers deserialize the result themselves.
        """
        cached = self.get(key)
        if cached is not None:
            return cached

        if not use_lock:
            value = loader()
            self.set(key, value, ttl)
            return value

        acquired = False
        lkey = lock_key(key)
        try:
            acquired = bool(self._client.set(lkey, "1", nx=True, px=settings.cache_lock_ttl_seconds * 1000))
        except redis.exceptions.RedisError:
            logger.warning("Redis lock acquire failed for key=%s, proceeding without lock", key, exc_info=True)
            acquired = True  # can't coordinate anyway - proceed like a normal miss

        if acquired:
            try:
                value = loader()
                self.set(key, value, ttl)
                return value
            finally:
                try:
                    self._client.delete(lkey)
                except redis.exceptions.RedisError:
                    pass

        # Another worker is populating the key - poll briefly, then give up
        # and compute directly rather than blocking the request.
        for _ in range(3):
            time.sleep(0.05)
            cached = self.get(key)
            if cached is not None:
                return cached

        return loader()
