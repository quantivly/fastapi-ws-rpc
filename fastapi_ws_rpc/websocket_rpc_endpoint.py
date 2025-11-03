from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

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
    A websocket RPC server endpoint, exposing RPC methods
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
        receive_timeout: float | None = None,
    ) -> None:
        """[summary]

        Args:
            methods (RpcMethodsBase): RPC methods to expose
            manager ([ConnectionManager], optional): Connection tracking object.
            Defaults to None (i.e. new ConnectionManager()).
            on_disconnect (List[coroutine], optional): Callbacks per disconnection
            on_connect(List[coroutine], optional): Callbacks per connection (Server
            spins the callbacks as a new task, not waiting on it.)
            frame_type (WebSocketFrameType, optional): Frame type for websocket messages
            serializing_socket_cls (type[SimpleWebSocket], optional): Socket wrapper class
            rpc_channel_get_remote_id (bool, optional): Whether to sync channel IDs
            max_message_size (int, optional): Maximum allowed message size in bytes (default 10MB).
                                            Prevents DoS attacks via extremely large JSON payloads.
            max_pending_requests (int, optional): Maximum number of pending RPC requests (default 1000).
                                                Prevents resource exhaustion from request flooding.
            max_connection_duration (float, optional): Maximum time in seconds that connections
                                                      can remain open. After this time, connections
                                                      will be gracefully closed. None means no limit.
            receive_timeout (float, optional): Timeout in seconds for receiving messages.
                                             If no message is received within this time, the connection
                                             is closed to prevent zombie connections. None means no timeout.
                                             Recommended: 300-600 seconds for most applications.
        """
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
        self._max_connection_duration = max_connection_duration
        self._receive_timeout = receive_timeout

    async def main_loop(
        self, websocket: WebSocket, client_id: str | None = None, **kwargs: Any
    ) -> None:
        try:
            await self.manager.connect(websocket)
            logger.info("Client connected", {"remote_address": websocket.client})
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
            channel = RpcChannel(
                self.methods,
                simple_websocket,
                sync_channel_id=self._rpc_channel_get_remote_id,
                max_pending_requests=self._max_pending_requests,
                max_connection_duration=self._max_connection_duration,
                **kwargs,
            )
            # register connect / disconnect handler
            channel.register_connect_handler(self._on_connect)
            channel.register_disconnect_handler(self._on_disconnect)
            # trigger connect handlers
            await channel.on_connect()
            try:
                while True:
                    # Apply receive timeout if configured to prevent zombie connections
                    if self._receive_timeout is not None:
                        data = await asyncio.wait_for(
                            simple_websocket.recv(), timeout=self._receive_timeout
                        )
                    else:
                        data = await simple_websocket.recv()
                    await channel.on_message(data)
            except asyncio.TimeoutError:
                client_port = websocket.client.port if websocket.client else "unknown"
                logger.info(
                    f"Connection timeout - no message received within {self._receive_timeout}s "
                    f"- {client_port} :: {channel.id}"
                )
                await self.handle_disconnect(websocket, channel)
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
