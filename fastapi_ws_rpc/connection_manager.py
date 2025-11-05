from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket


class ConnectionManager:
    """
    Manages active WebSocket connections for the RPC endpoint.

    This class provides connection lifecycle management for WebSocket RPC
    endpoints, tracking active connections and handling connection/disconnection
    events. It's designed for internal use by WebSocketRpcEndpoint.

    The implementation is simple and suitable for basic use cases. For production
    systems with high connection counts, complex requirements, or advanced
    features like connection pooling, rate limiting per connection, or
    distributed connection tracking, consider implementing a custom manager.

    Attributes
    ----------
    active_connections : list[WebSocket]
        List of currently active WebSocket connections.

    Notes
    -----
    - This class is thread-safe for single-process deployments
    - For multi-process/distributed deployments, use Redis or similar
    - Connection tracking is in-memory only (not persistent)
    - Primarily intended for internal use by WebSocketRpcEndpoint

    See Also
    --------
    WebSocketRpcEndpoint : Server endpoint that uses this manager.

    Examples
    --------
    Use with WebSocketRpcEndpoint (typical usage):

    >>> manager = ConnectionManager()
    >>> endpoint = WebSocketRpcEndpoint(methods, manager=manager)
    >>> endpoint.register_route(app, path="/ws")
    >>> # Later, check active connections
    >>> print(f"Active connections: {manager.get_connection_count()}")

    Custom connection manager for advanced use cases:

    >>> class RedisConnectionManager(ConnectionManager):
    ...     def __init__(self, redis_client):
    ...         super().__init__()
    ...         self.redis = redis_client
    ...
    ...     async def connect(self, websocket: WebSocket) -> None:
    ...         await super().connect(websocket)
    ...         # Store in Redis for distributed tracking
    ...         await self.redis.sadd("active_connections", str(websocket.client))
    """

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(
        self, websocket: WebSocket, subprotocol: str | None = None
    ) -> None:
        """
        Accept and register a new WebSocket connection.

        This method performs the WebSocket handshake by accepting the connection
        and adds it to the active connections list for tracking.

        Parameters
        ----------
        websocket : WebSocket
            The WebSocket connection to accept and register.
        subprotocol : str | None, optional
            WebSocket subprotocol to negotiate during the handshake.
            If None, no subprotocol negotiation is performed.

        Notes
        -----
        This method should be called before any RPC communication begins.
        The WebSocket handshake is completed during the accept() call.

        Examples
        --------
        >>> manager = ConnectionManager()
        >>> # In a FastAPI endpoint
        >>> @app.websocket("/ws")
        >>> async def websocket_endpoint(websocket: WebSocket):
        ...     await manager.connect(websocket)
        ...     # Now the connection is active and tracked

        >>> # With subprotocol negotiation
        >>> await manager.connect(websocket, subprotocol="jsonrpc2.0")
        """
        if subprotocol:
            await websocket.accept(subprotocol=subprotocol)
        else:
            await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        """
        Unregister a WebSocket connection from active tracking.

        Removes the connection from the active connections list. This method
        is safe to call multiple times or in race conditions - if the
        connection is not in the list, it silently succeeds.

        Parameters
        ----------
        websocket : WebSocket
            The WebSocket connection to remove from active connections.

        Notes
        -----
        This method does NOT close the WebSocket connection itself - it only
        removes it from tracking. The connection should already be closed or
        in the process of closing when this is called.

        Safe to call:
        - Multiple times for the same connection
        - For connections that were never added
        - Concurrently from different coroutines (race conditions)

        Examples
        --------
        >>> manager = ConnectionManager()
        >>> # After connection closes
        >>> manager.disconnect(websocket)
        >>> # Safe to call again
        >>> manager.disconnect(websocket)  # No error
        """
        # Connection already removed or never added - this is safe to ignore
        # as it can happen during race conditions or multiple disconnect calls
        with suppress(ValueError):
            self.active_connections.remove(websocket)

    def is_connected(self, websocket: WebSocket) -> bool:
        """
        Check if a websocket is currently tracked as active.

        Parameters
        ----------
        websocket : WebSocket
            The WebSocket connection to check.

        Returns
        -------
        bool
            True if the connection is in the active connections list,
            False otherwise.

        Notes
        -----
        This only checks if the connection is tracked by the manager.
        It does NOT verify if the underlying network connection is still
        alive or functioning. For connection health checks, use ping/pong
        mechanisms provided by RpcChannel.

        Examples
        --------
        >>> manager = ConnectionManager()
        >>> # After connecting
        >>> await manager.connect(websocket)
        >>> print(manager.is_connected(websocket))  # True
        >>> # After disconnecting
        >>> manager.disconnect(websocket)
        >>> print(manager.is_connected(websocket))  # False
        """
        return websocket in self.active_connections

    def get_connection_count(self) -> int:
        """
        Get the number of currently active connections.

        Returns
        -------
        int
            The count of active WebSocket connections being tracked.

        Notes
        -----
        Useful for monitoring, health checks, and capacity planning.
        This count represents connections tracked by this manager instance
        only - in multi-process deployments, each process has its own count.

        Examples
        --------
        >>> manager = ConnectionManager()
        >>> print(f"Active connections: {manager.get_connection_count()}")
        >>> # Use in health check endpoint
        >>> @app.get("/health")
        >>> async def health():
        ...     return {"active_connections": manager.get_connection_count()}
        """
        return len(self.active_connections)
