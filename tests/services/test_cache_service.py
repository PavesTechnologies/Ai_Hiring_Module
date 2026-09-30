import fnmatch

import redis

from app.core.cache_keys import lock_key, tombstone_key
from app.services.cache_service import CacheService


class FakeRedis:
    """Just enough of redis.Redis for CacheService, with a controllable server clock."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.clock = 1000.0

    def time(self):
        seconds = int(self.clock)
        return seconds, int(round((self.clock - seconds) * 1_000_000))

    def get(self, key):
        return self.store.get(key)

    def mget(self, keys):
        return [self.store.get(key) for key in keys]

    def set(self, key, value, ex=None, px=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)

    def scan(self, cursor=0, match="*", count=200):
        return 0, [key for key in self.store if fnmatch.fnmatchcase(key, match)]

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    def eval(self, script, numkeys, key, token):
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


class FakePipeline:
    def __init__(self, client):
        self.client, self.ops = client, []

    def set(self, *args, **kwargs):
        self.ops.append((args, kwargs))

    def execute(self):
        for args, kwargs in self.ops:
            self.client.set(*args, **kwargs)


class BrokenRedis:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise redis.exceptions.ConnectionError("down")
        return fail


KEY = "airs:resume:list:abc"


def test_miss_loads_and_caches():
    client = FakeRedis()
    service = CacheService(client)

    assert service.get_or_set(KEY, lambda: "fresh") == "fresh"
    assert client.store[KEY] == "fresh"
    assert service.get_or_set(KEY, lambda: "never called") == "fresh"


def test_read_racing_an_invalidation_does_not_cache_its_stale_result():
    client = FakeRedis()
    service = CacheService(client)

    def stale_loader():
        # A write commits and invalidates while this read is mid-load.
        client.clock += 0.5
        service.delete_by_prefix("airs:resume:list:")
        client.clock += 0.5
        return "stale"

    assert service.get_or_set(KEY, stale_loader) == "stale"
    assert KEY not in client.store


def test_exact_key_invalidation_also_blocks_a_racing_read():
    client = FakeRedis()
    service = CacheService(client)

    def stale_loader():
        client.clock += 0.1
        service.delete(KEY)
        return "stale"

    service.get_or_set(KEY, stale_loader)

    assert KEY not in client.store


def test_read_started_after_invalidation_is_cached_normally():
    client = FakeRedis()
    service = CacheService(client)
    service.delete_by_prefix("airs:resume:list:")
    client.clock += 1

    service.get_or_set(KEY, lambda: "fresh")

    assert client.store[KEY] == "fresh"
    assert tombstone_key("airs:resume:list:") in client.store


def test_unrelated_invalidation_does_not_block_caching():
    client = FakeRedis()
    service = CacheService(client)

    def loader():
        service.delete_by_prefix("airs:jd:list:")
        return "fresh"

    service.get_or_set(KEY, loader)

    assert client.store[KEY] == "fresh"


def test_lock_is_released_by_its_owner():
    client = FakeRedis()
    CacheService(client).get_or_set(KEY, lambda: "fresh")

    assert lock_key(KEY) not in client.store


def test_expired_lock_retaken_by_another_worker_is_not_released_by_us():
    client = FakeRedis()
    service = CacheService(client)

    def loader():
        # Our lock expired mid-load and another worker took it.
        client.store[lock_key(KEY)] = "someone-else"
        return "fresh"

    service.get_or_set(KEY, loader)

    assert client.store[lock_key(KEY)] == "someone-else"


def test_redis_down_falls_back_to_loader_without_raising():
    service = CacheService(BrokenRedis())

    assert service.get_or_set(KEY, lambda: "from-db") == "from-db"
    service.delete(KEY)
    service.delete_by_prefix("airs:resume:list:")
