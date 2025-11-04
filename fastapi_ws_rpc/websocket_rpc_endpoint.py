from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .config import RpcConnectionConfig
from .connection_manager import ConnectionManager
from .logger import get_logger
from .rpc_channel import OnConnectCallback, OnDisconnectCallback, RpcChannel
from .rpc_methods import RpcMethodsBase
from .schemas import WebSocketFrameType
from .simplewebsocket import JsonSerializingWebSocket, SimpleWebSocket

logger = get_logger("RPC_ENDPOINT")


class WebSocketSimplifier(SimpleWebSocket):
    """
    Simple wrapper over FastAPI WebSocket to ensure unified interface for send/recv
    """

    def __init__(
        self,
        websocket: WebSocket,
        frame_type: WebSocketFrameType = WebSocketFrameType.Text,
    ) -> None:
        self.websocket = websocket
        self.frame_type = frame_type

    @property
    def send(self) -> Any:
        if self.frame_type == WebSocketFrameType.Binary:
            return self.websocket.send_bytes
        return self.websocket.send_text

    @property
    def recv(self) -> Any:
        if self.frame_type == WebSocketFrameType.Binary:
            return self.websocket.receive_bytes
        return self.websocket.receive_text

    async def close(self, code: int = 1000) -> None:
        await self.websocket.close(code)


class WebSocketRpcEndpoint:
    """
    A websocket RPC server endpoint, exposing RPC methods.

    This endpoint manages WebSocket connections and exposes RPC methods to clients.
    It supports connection lifecycle management, message size limits, idle timeout,
    and graceful connection closure.

    Parameters
    ----------
    methods : RpcMethodsBase | None, optional
        RPC methods to expose to clients. If None, creates empty RpcMethodsBase.
    manager : ConnectionManager | None, optional
        Connection tracking object. If None, creates new ConnectionManager.
    on_disconnect : list[OnDisconnectCallback] | None, optional
        Callbacks to execute on client disconnection.
    on_connect : list[OnConnectCallback] | None, optional
        Callbacks to execute on client connection. Server spins these as
        new tasks without waiting.
    frame_type : WebSocketFrameType, default WebSocketFrameType.Text
        Frame type for websocket messages (Text or Binary).
    serializing_socket_cls : type[SimpleWebSocket], default JsonSerializingWebSocket
        Socket wrapper class for message serialization.
    rpc_channel_get_remote_id : bool, default False
        Whether to sync channel IDs between client and server.
    max_message_size : int | None, optional
        Maximum allowed message size in bytes. Prevents DoS attacks via
        extremely large JSON payloads. None means no limit.
    max_pending_requests : int | None, optional
        Maximum number of pending RPC requests. Prevents resource exhaustion
        from request flooding. None means no limit.
    max_connection_duration : float | None, optional
        Maximum time in seconds that connections can remain open. After this
        time, connections will be gracefully closed. None means no limit.
        DEPRECATED: Use connection_config.max_connection_duration instead.
    connection_config : RpcConnectionConfig | None, optional
        Connection lifecycle configuration including:
        - idle_timeout: Close connection if no messages received for this duration.
          The timer resets whenever ANY message is received. None means no idle timeout.
        - max_connection_duration: Maximum connection lifetime regardless of activity.
        - default_response_timeout: Default timeout for RPC responses.
        If None, creates default RpcConnectionConfig with no timeouts.

    Notes
    -----
    The idle_timeout parameter provides TRUE idle semantics - the connection is
    closed only if NO messages are received within the timeout period. This is
    different from the old receive_timeout which applied per-message and had a
    misleading name.

    Examples
    --------
    >>> # Create endpoint with 5-minute idle timeout
    >>> config = RpcConnectionConfig(idle_timeout=300.0)
    >>> endpoint = WebSocketRpcEndpoint(
    ...     methods=my_methods,
    ...     connection_config=config
    ... )
    """

    def __init__(
        self,
        methods: RpcMethodsBase | None = None,
        manager: ConnectionManager | None = None,
        on_disconnect: list[OnDisconnectCallback] | None = None,
        on_connect: list[OnConnectCallback] | None = None,
        frame_type: WebSocketFrameType = WebSocketFrameType.Text,
        serializing_socket_cls: type[SimpleWebSocket] = JsonSerializingWebSocket,
        rpc_channel_get_remote_id: bool = False,
        max_message_size: int | None = None,
        max_pending_requests: int | None = None,
        max_connection_duration: float | None = None,
        connection_config: RpcConnectionConfig | None = None,
    ) -> None:
        self.manager = manager if manager is not None else ConnectionManager()
        self.methods = methods if methods is not None else RpcMethodsBase()
        # Event handlers
        self._on_disconnect = on_disconnect
        self._on_connect = on_connect
        self._frame_type = frame_type
        self._serializing_socket_cls = serializing_socket_cls
        self._rpc_channel_get_remote_id = rpc_channel_get_remote_id
        self._max_message_size = max_message_size
        self._max_pending_requests = max_pending_requests

        # Connection configuration: merge legacy and new config
        if connection_config is None:
            # Create default config, potentially overridden by legacy parameter
            self.connection_config = RpcConnectionConfig(
                max_connection_duration=max_connection_duration
            )
        else:
            # Use provided config, but warn if legacy parameter also provided
            if max_connection_duration is not None:
                logger.warning(
                    "Both max_connection_duration and connection_config provided. "
                    "Using connection_config.max_connection_duration. "
                    "Please migrate to connection_config only."
                )
            self.connection_config = connection_config

        # For backward compatibility with RpcChannel
        self._max_connection_duration = self.connection_config.max_connection_duration

    async def main_loop(
        self, websocket: WebSocket, client_id: str | None = None, **kwargs: Any
    ) -> None:
        """
        Main loop for receiving and processing WebSocket messages.

        Implements true idle timeout semantics where the connection is closed only
        if NO messages are received within the idle_timeout period. The idle timer
        resets whenever any message is received.

        Parameters
        ----------
        websocket : WebSocket
            FastAPI WebSocket connection.
        client_id : str | None, optional
            Optional client identifier.
        **kwargs : Any
            Additional keyword arguments passed to RpcChannel.

        Notes
        -----
        Idle timeout behavior:
        - If idle_timeout is set, tracks time since last message
        - Timer resets on EVERY message received
        - Connection closes gracefully when idle timeout expires
        - Different from per-message timeout (old behavior)
        """
        try:
            # Accept WebSocket connection with subprotocol negotiation
            subprotocols = (
                self.connection_config.websocket.subprotocols
                if self.connection_config.websocket.subprotocols
                else None
            )

            # Pass subprotocol to manager for accept() call
            if subprotocols:
                await self.manager.connect(websocket, subprotocol=subprotocols[0])
                logger.info(
                    "Client connected with subprotocol: %s",
                    subprotocols[0],
                    {"remote_address": websocket.client},
                )
            else:
                await self.manager.connect(websocket)
                logger.info(
                    "Client connected without subprotocol",
                    {"remote_address": websocket.client},
                )
            # Create WebSocket wrapper with optional message size limit
            if self._max_message_size is not None:
                simple_websocket = self._serializing_socket_cls(
                    WebSocketSimplifier(websocket, frame_type=self._frame_type),
                    max_message_size=self._max_message_size,
                )  # type: ignore[call-arg]
            else:
                simple_websocket = self._serializing_socket_cls(
                    WebSocketSimplifier(websocket, frame_type=self._frame_type)
                )  # type: ignore[call-arg]
            # Store negotiated subprotocol from FastAPI WebSocket
            # The subprotocol is available after accept() is called
            negotiated_subprotocol = (
                getattr(websocket, "scope", {}).get("subprotocols", [None])[0]
                if subprotocols
                else None
            )

            channel = RpcChannel(
                self.methods,
                simple_websocket,
                sync_channel_id=self._rpc_channel_get_remote_id,
                max_pending_requests=self._max_pending_requests,
                max_connection_duration=self._max_connection_duration,
                debug_config=self.connection_config.debug,
                subprotocol=negotiated_subprotocol,
                connect_callbacks=self._on_connect,
                disconnect_callbacks=self._on_disconnect,
                **kwargs,
            )
            # trigger connect handlers
            await channel.on_connect()
            try:
                # Initialize idle timeout tracking
                last_message_time = time.time()
                idle_timeout = self.connection_config.idle_timeout

                while True:
                    # Calculate remaining time before idle timeout
                    recv_timeout: float | None = None
                    if idle_timeout is not None:
                        time_since_last_message = time.time() - last_message_time
                        remaining_time = idle_timeout - time_since_last_message

                        if remaining_time <= 0:
                            # Idle timeout expired
                            client_port = (
                                websocket.client.port if websocket.client else "unknown"
                            )
                            logger.info(
                                "Connection idle timeout: no messages received for %.1fs "
                                "- %s :: %s",
                                idle_timeout,
                                client_port,
                                channel.id,
                            )
                            await self.handle_disconnect(websocket, channel)
                            break

                        recv_timeout = remaining_time

                    # Receive message with calculated timeout
                    try:
                        if recv_timeout is not None:
                            data = await asyncio.wait_for(
                                simple_websocket.recv(), timeout=recv_timeout
                            )
                        else:
                            data = await simple_websocket.recv()
                    except asyncio.TimeoutError:
                        # Idle timeout reached
                        client_port = (
                            websocket.client.port if websocket.client else "unknown"
                        )
                        logger.info(
                            "Connection idle timeout reached - %s :: %s",
                            client_port,
                            channel.id,
                        )
                        await self.handle_disconnect(websocket, channel)
                        break

                    # Message received - reset idle timer
                    last_message_time = time.time()

                    # Process message
                    await channel.on_message(data)

            except WebSocketDisconnect:
                client_port = websocket.client.port if websocket.client else "unknown"
                logger.info(f"Client disconnected - {client_port} :: {channel.id}")
                await self.handle_disconnect(websocket, channel)
            except (
                RuntimeError,
                ConnectionError,
                OSError,
                asyncio.CancelledError,
            ) as e:
                # Cover cases like RuntimeError('Cannot call "send" once a close
                # message has been sent.') and various connection failures
                client_port = websocket.client.port if websocket.client else "unknown"
                logger.info(
                    f"Client connection failed - {client_port} :: {channel.id}: "
                    f"{type(e).__name__}: {e}"
                )
                await self.handle_disconnect(websocket, channel)
        except (RuntimeError, ConnectionError, OSError, ValueError, TypeError) as e:
            # Handle initialization and connection setup failures
            client_port = websocket.client.port if websocket.client else "unknown"
            logger.exception(f"Failed to serve - {client_port}: {type(e).__name__}")
            self.manager.disconnect(websocket)

    async def handle_disconnect(
        self, websocket: WebSocket, channel: RpcChannel
    ) -> None:
        self.manager.disconnect(websocket)
        await channel.on_disconnect()

    async def on_connect(self, channel: RpcChannel, websocket: WebSocket) -> None:
        """
        Called upon new client connection
        """
        # Trigger connect callback if available
        if self._on_connect is not None:
            for callback in self._on_connect:
                asyncio.create_task(callback(channel))  # type: ignore[arg-type]

    def register_route(self, router: Any, path: str = "/ws") -> None:
        """
        Register this endpoint as a default websocket route on the given router
        Args:
            router: FastAPI router to load route onto
            path (str, optional): the route path. Defaults to "/ws".
        """

        @router.websocket(path)  # type: ignore[misc]
        async def websocket_endpoint(websocket: WebSocket) -> None:
            await self.main_loop(websocket)
