import asyncio
import json
import logging

from app.webcore.redis import create_pubsub_client
from app.websocket.manager import manager

logger = logging.getLogger(__name__)


class RedisSubscriber:

    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}

    async def subscribe(self, channel: str) -> None:
        """
        Start a Redis Pub/Sub listener for a channel.

        One listener is created per active channel.
        """

        if channel in self.tasks:
            return

        task = asyncio.create_task(
            self._listen(channel)
        )

        self.tasks[channel] = task

        logger.info(
            "Redis subscriber started. channel=%s",
            channel,
        )

    async def unsubscribe(self, channel: str) -> None:

        task = self.tasks.pop(channel, None)

        if not task:
            return

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        logger.info(
            "Redis subscriber stopped. channel=%s",
            channel,
        )

    async def _listen(self, channel: str) -> None:

        redis_client = create_pubsub_client()
        pubsub = redis_client.pubsub()

        try:
            await asyncio.to_thread(
                pubsub.subscribe,
                channel,
            )

            logger.info(
                "Subscribed to Redis channel=%s",
                channel,
            )

            while True:

                message = await asyncio.to_thread(
                    pubsub.get_message,
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )

                if message is None:
                    await asyncio.sleep(0.01)
                    continue

                if message["type"] != "message":
                    continue

                try:
                    event = json.loads(message["data"])

                    await manager.send_to_channel(
                        channel,
                        event,
                    )

                except json.JSONDecodeError:
                    logger.exception(
                        "Invalid WebSocket event received from Redis. "
                        "channel=%s",
                        channel,
                    )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Redis subscriber failed. channel=%s",
                channel,
            )

        finally:

            try:
                await asyncio.to_thread(
                    pubsub.unsubscribe,
                    channel,
                )
            except Exception:
                pass

            pubsub.close()
            redis_client.close()

            logger.info(
                "Redis subscriber cleaned up. channel=%s",
                channel,
            )


redis_subscriber = RedisSubscriber()