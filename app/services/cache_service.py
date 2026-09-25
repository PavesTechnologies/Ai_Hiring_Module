import logging
import time
import uuid
from typing import Callable, Optional

import redis

from app.core.cache_keys import lock_key, tombstone_key
from app.core.config import settings

logger = logging.getLogger(__name__)

# How long an invalidation is remembered. Must exceed the slowest cache
# loader, so a read that started before the invalidation can never write
# its (pre-change) result back afterwards.
_TOMBSTONE_TTL_SECONDS = 30

# Deletes the lock only if it still holds this caller's token - never a lock
# another worker re-acquired after ours expired.
_RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class CacheService:
    """Cache-aside helper over Redis.

    Every Redis call is guarded: if Redis is unreachable or errors, methods
    log a warning and behave as a cache miss / no-op so callers always fall
    back to PostgreSQL instead of failing the request.

    Invalidations leave a short-lived tombstone (Redis server time). A
    loader that started before a matching invalidation does not cache its
    result - otherwise a read racing a write (read DB -> write commits ->
    write invalidates -> read caches) would pin pre-change data for the
    whole TTL.
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
        self._write_tombstones(keys)

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
        self._write_tombstones((prefix,))

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
            return self._load_and_store(key, loader, ttl)

        lkey = lock_key(key)
        token = uuid.uuid4().hex
        try:
            acquired = bool(self._client.set(lkey, token, nx=True, px=settings.cache_lock_ttl_seconds * 1000))
        except redis.exceptions.RedisError:
            logger.warning("Redis lock acquire failed for key=%s, proceeding without lock", key, exc_info=True)
            return loader()

        if acquired:
            try:
                return self._load_and_store(key, loader, ttl)
            finally:
                try:
                    self._client.eval(_RELEASE_LOCK_SCRIPT, 1, lkey, token)
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

    def _load_and_store(self, key: str, loader: Callable[[], str], ttl: Optional[int]) -> str:
        started_at = self._server_time()
        value = loader()
        if started_at is not None and not self._invalidated_since(key, started_at):
            self.set(key, value, ttl)
        return value

    def _server_time(self) -> Optional[float]:
        try:
            seconds, microseconds = self._client.time()
            return seconds + microseconds / 1_000_000
        except redis.exceptions.RedisError:
            return None

    def _invalidated_since(self, key: str, started_at: float) -> bool:
        """True if `key`, or any `:`-delimited prefix of it, was invalidated at or after started_at."""
        targets = [key[: index + 1] for index, char in enumerate(key) if char == ":"] + [key]
        try:
            stamps = self._client.mget([tombstone_key(target) for target in targets])
        except redis.exceptions.RedisError:
            return True
        return any(stamp is not None and float(stamp) >= started_at for stamp in stamps)

    def _write_tombstones(self, targets) -> None:
        now = self._server_time()
        if now is None:
            return
        try:
            pipe = self._client.pipeline(transaction=False)
            for target in targets:
                pipe.set(tombstone_key(target), repr(now), ex=_TOMBSTONE_TTL_SECONDS)
            pipe.execute()
        except redis.exceptions.RedisError:
            logger.warning("Redis tombstone write failed for targets=%s", targets, exc_info=True)
