# connection_manager.py

from typing import Dict, Set
from fastapi import WebSocket
import json
import logging
import asyncio
import redis.asyncio as redis

logger = logging.getLogger(__name__)

# connection_manager.py

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, contact_id: int):
        await websocket.accept()
        if contact_id not in self.active_connections:
            self.active_connections[contact_id] = set()
        self.active_connections[contact_id].add(websocket)
        logger.info(f"WebSocket connection established for contact_id {contact_id}.")

    def disconnect(self, websocket: WebSocket, contact_id: int):
        if contact_id in self.active_connections:
            self.active_connections[contact_id].discard(websocket)
            if not self.active_connections[contact_id]:
                del self.active_connections[contact_id]
            logger.info(f"WebSocket connection closed for contact_id {contact_id}.")

    async def broadcast(self, contact_id: int, message: str):
        if contact_id in self.active_connections:
            logger.info(f"Broadcasting message to {len(self.active_connections[contact_id])} clients for contact_id {contact_id}.")
            for connection in self.active_connections[contact_id]:
                try:
                    await connection.send_text(message)
                except Exception as e:
                    logger.error(f"Failed to send message to a client: {e}")
