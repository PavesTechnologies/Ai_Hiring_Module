import asyncio
import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(
        self,
        channel: str,
        websocket: WebSocket,
    ) -> None:
        await websocket.accept()

        self.connections[channel].add(websocket)

        logger.info(
            "WebSocket connected. channel=%s active_connections=%s",
            channel,
            len(self.connections[channel]),
        )

    def disconnect(
        self,
        channel: str,
        websocket: WebSocket,
    ) -> None:
        connections = self.connections.get(channel)

        if not connections:
            return

        connections.discard(websocket)

        if not connections:
            self.connections.pop(channel, None)

        logger.info(
            "WebSocket disconnected. channel=%s",
            channel,
        )

    async def send_to_channel(
        self,
        channel: str,
        message: dict,
    ) -> None:
        connections = self.connections.get(channel)

        if not connections:
            return

        # Concurrently, so one slow client never delays the rest of the channel.
        targets = list(connections)
        results = await asyncio.gather(
            *(websocket.send_json(message) for websocket in targets),
            return_exceptions=True,
        )

        for websocket, result in zip(targets, results):
            if isinstance(result, Exception):
                logger.warning(
                    "Failed to send WebSocket message; dropping connection. channel=%s error=%r",
                    channel,
                    result,
                )
                self.disconnect(channel, websocket)

    def has_connections(self, channel: str) -> bool:
        return bool(self.connections.get(channel))

    def connection_count(self, channel: str) -> int:
        return len(self.connections.get(channel, set()))


manager = ConnectionManager()