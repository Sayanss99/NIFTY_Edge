"""WebSocket connection manager for real-time data push."""

from fastapi import WebSocket
import json
import numpy as np


def _json_default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def _dumps(message):
    return json.dumps(message, default=_json_default)


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, list[WebSocket]] = {}  # user_id -> [ws]

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: int):
        if user_id in self.active_connections:
            self.active_connections[user_id] = [
                ws for ws in self.active_connections[user_id] if ws != websocket
            ]
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def broadcast(self, message: dict):
        """Send to ALL connected users."""
        data = _dumps(message)
        dead = []
        for user_id, connections in self.active_connections.items():
            for ws in connections:
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append((user_id, ws))
        for user_id, ws in dead:
            self.disconnect(ws, user_id)

    async def send_to_user(self, user_id: int, message: dict):
        """Send to a specific user's connections."""
        if user_id not in self.active_connections:
            return
        data = _dumps(message)
        dead = []
        for ws in self.active_connections[user_id]:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, user_id)

    @property
    def connected_user_ids(self) -> list[int]:
        return list(self.active_connections.keys())


manager = ConnectionManager()
