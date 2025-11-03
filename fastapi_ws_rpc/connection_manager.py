from contextlib import suppress

from fastapi import WebSocket


class ConnectionManager:
    """
    Manages active WebSocket connections for the RPC endpoint.

    Tracks connected clients and provides connection lifecycle management.
    This is a simple implementation suitable for basic use cases. For production
    systems with high connection counts or complex requirements, consider
    implementing a more sophisticated connection manager.
    """

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        """
        Accept and register a new WebSocket connection.

        Parameters
        ----------
        websocket : WebSocket
            The WebSocket connection to accept and register.
        """
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        """
        Unregister a WebSocket connection.

        Parameters
        ----------
        websocket : WebSocket
            The WebSocket connection to remove from active connections.

        Notes
        -----
        Silently ignores if the connection is not in the active list, making
        this method safe to call multiple times or in race conditions.
        """
        # Connection already removed or never added - this is safe to ignore
        # as it can happen during race conditions or multiple disconnect calls
        with suppress(ValueError):
            self.active_connections.remove(websocket)

    def is_connected(self, websocket: WebSocket) -> bool:
        """
        Check if a websocket is in active connections.

        Parameters
        ----------
        websocket : WebSocket
            The WebSocket connection to check.

        Returns
        -------
        bool
            True if the connection is active, False otherwise.
        """
        return websocket in self.active_connections
