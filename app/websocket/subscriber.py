import asyncio
import json
import logging

from app.webcore.redis import create_async_pubsub_client
from app.websocket.manager import manager

logger = logging.getLogger(__name__)

_RECONNECT_INITIAL_DELAY_SECONDS = 1.0
_RECONNECT_MAX_DELAY_SECONDS = 30.0


class RedisSubscriber:
    """
    One asyncio listener task per channel that currently has WebSocket
    clients. Listeners reconnect with backoff when Redis drops, so a Redis
    restart pauses delivery instead of silently killing the channel for good.
    """

    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}

    async def subscribe(self, channel: str) -> None:
        """Start the channel's listener unless a live one is already running."""
        task = self.tasks.get(channel)
        if task is not None and not task.done():
            return

        self.tasks[channel] = asyncio.create_task(self._listen(channel))
        logger.info("Redis subscriber started. channel=%s", channel)

    async def unsubscribe(self, channel: str) -> None:
        """Stop the listener - unless a client (re)joined the channel meanwhile."""
        if manager.has_connections(channel):
            return

        task = self.tasks.pop(channel, None)
        if task is None:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Redis subscriber stopped. channel=%s", channel)

    async def _listen(self, channel: str) -> None:
        delay = _RECONNECT_INITIAL_DELAY_SECONDS
        while True:
            client = create_async_pubsub_client()
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(channel)
                logger.info("Subscribed to Redis channel=%s", channel)
                delay = _RECONNECT_INITIAL_DELAY_SECONDS

                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        event = json.loads(message["data"])
                    except json.JSONDecodeError:
                        logger.exception("Invalid WebSocket event received from Redis. channel=%s", channel)
                        continue
                    await manager.send_to_channel(channel, event)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Redis subscriber lost its connection. channel=%s - reconnecting in %.0fs", channel, delay,
                )
            finally:
                await self._close_quietly(pubsub, client)

            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY_SECONDS)

    @staticmethod
    async def _close_quietly(pubsub, client) -> None:
        try:
            await pubsub.aclose()
        except Exception:
            pass
        try:
            await client.aclose()
        except Exception:
            pass


redis_subscriber = RedisSubscriber()
