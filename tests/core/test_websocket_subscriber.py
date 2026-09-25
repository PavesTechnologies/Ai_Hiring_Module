import asyncio
import json
from unittest.mock import AsyncMock, patch

import redis

from app.websocket import subscriber as subscriber_module
from app.websocket.subscriber import RedisSubscriber

CHANNEL = "airs:board:test"


class FakePubSub:
    """First connection drops; the reconnected one delivers a message and then idles."""

    def __init__(self, connection_number: int):
        self.connection_number = connection_number

    async def subscribe(self, channel):
        pass

    async def listen(self):
        if self.connection_number == 1:
            raise redis.exceptions.ConnectionError("redis restarted")
            yield  # pragma: no cover - makes this an async generator
        yield {"type": "subscribe", "data": 1}
        yield {"type": "message", "data": json.dumps({"event": "board.candidate_updated"})}
        await asyncio.Event().wait()

    async def aclose(self):
        pass


class FakeClient:
    connections = 0

    def pubsub(self, ignore_subscribe_messages=True):
        FakeClient.connections += 1
        return FakePubSub(FakeClient.connections)

    async def aclose(self):
        pass


def test_listener_reconnects_after_redis_drops_and_keeps_delivering():
    async def scenario():
        FakeClient.connections = 0
        send = AsyncMock()
        subscriber = RedisSubscriber()
        with patch.object(subscriber_module, "create_async_pubsub_client", FakeClient), \
             patch.object(subscriber_module, "_RECONNECT_INITIAL_DELAY_SECONDS", 0), \
             patch.object(subscriber_module.manager, "send_to_channel", send), \
             patch.object(subscriber_module.manager, "has_connections", return_value=False):
            await subscriber.subscribe(CHANNEL)
            for _ in range(50):
                if send.await_count:
                    break
                await asyncio.sleep(0.01)
            await subscriber.unsubscribe(CHANNEL)
        return send, FakeClient.connections, subscriber

    send, connections, subscriber = asyncio.run(scenario())

    assert connections == 2
    send.assert_awaited_once_with(CHANNEL, {"event": "board.candidate_updated"})
    assert CHANNEL not in subscriber.tasks


def test_subscribe_replaces_a_finished_listener_instead_of_no_op():
    async def scenario():
        subscriber = RedisSubscriber()
        finished = asyncio.get_running_loop().create_future()
        finished.set_result(None)
        subscriber.tasks[CHANNEL] = finished
        with patch.object(subscriber, "_listen", AsyncMock()):
            await subscriber.subscribe(CHANNEL)
            replaced = subscriber.tasks[CHANNEL] is not finished
            await subscriber.tasks[CHANNEL]
        return replaced

    assert asyncio.run(scenario()) is True


def test_unsubscribe_keeps_listener_when_a_client_rejoined():
    async def scenario():
        subscriber = RedisSubscriber()
        task = asyncio.get_running_loop().create_future()
        subscriber.tasks[CHANNEL] = task
        with patch.object(subscriber_module.manager, "has_connections", return_value=True):
            await subscriber.unsubscribe(CHANNEL)
        still_there = subscriber.tasks.get(CHANNEL) is task and not task.cancelled()
        task.cancel()
        return still_there

    assert asyncio.run(scenario()) is True
