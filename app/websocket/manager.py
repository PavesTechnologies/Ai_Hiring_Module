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

        disconnected = []

        for websocket in connections.copy():
            try:
                await websocket.send_json(message)
            except Exception:
                logger.exception(
                    "Failed to send WebSocket message. channel=%s",
                    channel,
                )
                disconnected.append(websocket)

        for websocket in disconnected:
            self.disconnect(channel, websocket)

    def has_connections(self, channel: str) -> bool:
        return bool(self.connections.get(channel))

    def connection_count(self, channel: str) -> int:
        return len(self.connections.get(channel, set()))


manager = ConnectionManager()