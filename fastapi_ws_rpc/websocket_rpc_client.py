"""
WebSocketRpcClient module provides a client that connects to a WebSocketRpcEndpoint
via websocket and enables bi-directional RPC calls.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from typing import Any

import websockets
from pydantic import ValidationError
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidStatus,
    WebSocketException,
)

from ._internal.rpc_keepalive import RpcKeepalive
from ._internal.rpc_retry import RpcRetryManager
from .config import WebSocketRpcClientConfig
from .exceptions import (
    RpcBackpressureError,
    RpcChannelClosedError,
    RpcInvalidStateError,
)
from .logger import get_logger
from .rpc_channel import OnConnectCallback, OnDisconnectCallback, RpcChannel
from .rpc_methods import RpcMethodsBase
from .simplewebsocket import JsonSerializingWebSocket, SimpleWebSocket

# Type alias for error callbacks
# Note: WebSocketRpcClient maps on_error to on_disconnect for simplicity.
# For granular error handling, use RpcChannel directly with error_callbacks.
OnErrorCallback = OnDisconnectCallback

logger = get_logger(__name__)

# Security: Maximum time to receive a complete WebSocket message
# Protects against Slowloris-style attacks where malicious servers send
# partial frames slowly to tie up client resources.
#
# How it works:
# - The websockets library buffers incoming frames and assembles them into messages
# - recv() blocks until a complete message is received OR this timeout expires
# - If server sends a 10MB message at 1KB/s (taking ~10,000s), timeout triggers after 60s
# - This protects against both slow-send attacks and incomplete message attacks
#
# Note: This timeout applies to the entire message reception, not individual frames
MESSAGE_RECEIVE_TIMEOUT = 60.0  # 1 minute max to receive complete message

# WebSocket close codes (RFC 6455 Section 7.4 and IANA WebSocket Close Code Registry)
# See: https://www.rfc-editor.org/rfc/rfc6455.html#section-7.4
# See: https://www.iana.org/assignments/websocket/websocket.xml
WS_CLOSE_CODE_NORMAL = 1000  # Normal closure
WS_CLOSE_CODE_GOING_AWAY = 1001  # Server shutting down or browser navigating
WS_CLOSE_CODE_PROTOCOL_ERROR = 1002  # Protocol/implementation violation
WS_CLOSE_CODE_UNSUPPORTED_DATA = 1003  # Data type not acceptable
WS_CLOSE_CODE_ABNORMAL_CLOSURE = 1006  # No close frame received (network failure)
WS_CLOSE_CODE_INVALID_DATA = 1007  # Inconsistent/malformed data
WS_CLOSE_CODE_POLICY_VIOLATION = 1008  # Generic policy violation
WS_CLOSE_CODE_INTERNAL_ERROR = 1011  # Unexpected server condition
WS_CLOSE_CODE_SERVICE_RESTART = 1012  # Server restarting (reconnection encouraged)


class WebSocketRpcClient:
    """
    RPC client for connecting to a WebSocketRpcEndpoint server.

    This client manages the WebSocket connection lifecycle, enables bidirectional
    RPC communication, and handles automatic reconnection with configurable retry
    policies.

    The client can:
    - Call methods exposed by the server
    - Expose methods that the server can call
    - Automatically reconnect with exponential backoff
    - Maintain connection with keep-alive pings

    Configuration is provided through the WebSocketRpcClientConfig object,
    which provides a clean, validated interface for all client settings.
    """

    def __init__(
        self,
        uri: str,
        methods: RpcMethodsBase | None = None,
        config: WebSocketRpcClientConfig | None = None,
        on_connect: list[OnConnectCallback] | None = None,
        on_disconnect: list[OnDisconnectCallback] | None = None,
        on_error: list[OnErrorCallback] | None = None,
        serializing_socket_cls: type[SimpleWebSocket] = JsonSerializingWebSocket,
    ) -> None:
        """Initialize the WebSocketRpcClient.

        Parameters
        ----------
        uri : str
            Server URI to connect to (e.g., 'ws://localhost/ws/client1').
        methods : RpcMethodsBase | None, optional
            RPC methods to expose to the server. Defaults to an empty
            RpcMethodsBase.
        config : WebSocketRpcClientConfig | None, optional
            Configuration object for all client behavior. If None, uses
            default configuration. Use WebSocketRpcClientConfig.production_defaults()
            or .development_defaults() for common presets.
        on_connect : list[OnConnectCallback] | None, optional
            Callbacks executed when connection is established.
        on_disconnect : list[OnDisconnectCallback] | None, optional
            Callbacks executed when connection is lost.
        on_error : list[OnErrorCallback] | None, optional
            Callbacks executed when errors occur (currently maps to on_disconnect).
        serializing_socket_cls : type[SimpleWebSocket], optional
            Class for serializing/deserializing messages, by default JsonSerializingWebSocket.

        Examples
        --------
        Using default config::

            async with WebSocketRpcClient(uri, RpcUtilityMethods()) as client:
                response = await client.call("echo", {'text': "Hello World!"})
                print(response)

        Using production config with custom settings::

            from fastapi_ws_rpc.config import WebSocketRpcClientConfig, RpcKeepaliveConfig
            config = WebSocketRpcClientConfig(
                keepalive=RpcKeepaliveConfig(interval=30.0, use_protocol_ping=True)
            )
            async with WebSocketRpcClient(uri, methods, config=config) as client:
                await client.call("some_method")

        Using preset configurations::

            config = WebSocketRpcClientConfig.production_defaults()
            async with WebSocketRpcClient(uri, methods, config=config) as client:
                await client.call("some_method")
        """
        self.uri = uri
        self.methods = methods or RpcMethodsBase()

        # Initialize and validate configuration
        self.config = config or WebSocketRpcClientConfig()
        self.config.validate()

        # Store frequently accessed config values as instance attributes for convenience
        self._max_message_size = self.config.backpressure.max_message_size
        self._max_pending_requests = self.config.backpressure.max_pending_requests
        self._default_response_timeout = self.config.connection.default_response_timeout
        self._max_connection_duration = self.config.connection.max_connection_duration

        # Build websocket connection kwargs from config
        self.connect_kwargs: dict[str, Any] = self.config.websocket_kwargs.copy()
        # Set default open timeout if not specified
        self.connect_kwargs.setdefault("open_timeout", 30)

        # State variables
        self.ws: SimpleWebSocket | None = None
        self.channel: RpcChannel | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._connection_closed = asyncio.Event()
        self._closing = False  # Flag to make close() idempotent
        self._close_lock = asyncio.Lock()  # Lock to prevent race conditions in close()
        self._reconnect_lock = (
            asyncio.Lock()
        )  # Lock to prevent reconnection race conditions
        self._close_code: int | None = None  # WebSocket close code
        self._close_reason: str | None = None  # WebSocket close reason

        # Keep-alive configuration (manager will be created on connection)
        self._keepalive: RpcKeepalive | None = None

        # Retry configuration - convert from config to RpcRetryManager format
        if not self.config.retry.enabled:
            # Retries disabled
            self._retry_manager = RpcRetryManager(False)
        else:
            # Convert RpcRetryConfig to tenacity-compatible dict
            from tenacity import retry_if_exception, stop, wait

            retry_config_dict = {
                "wait": (
                    wait.wait_random_exponential(
                        min=self.config.retry.min_wait, max=self.config.retry.max_wait
                    )
                    if self.config.retry.jitter
                    else wait.wait_exponential(
                        min=self.config.retry.min_wait, max=self.config.retry.max_wait
                    )
                ),
                "stop": stop.stop_after_attempt(self.config.retry.max_attempts),
                "retry": retry_if_exception(
                    lambda e: True
                ),  # Retry all exceptions by default
                "reraise": True,
            }
            self._retry_manager = RpcRetryManager(retry_config_dict)

        # Event handlers
        self._on_disconnect: list[OnDisconnectCallback] = on_disconnect or []
        self._on_connect: list[OnConnectCallback] = on_connect or []
        # on_error callbacks are added to on_disconnect list (unified error/disconnect handling)
        # For separate error handling, use RpcChannel.error_callbacks directly
        if on_error:
            self._on_disconnect.extend(on_error)

        # Serialization
        self._serializing_socket_cls = serializing_socket_cls

        # Reconnection state tracking
        self._reconnect_attempt = 0  # Current reconnection attempt number
        self._is_reconnecting = (
            False  # Flag to prevent concurrent reconnection attempts
        )
        # Note: self._reconnect_lock is initialized at line 147 with other asyncio locks

    def _validate_connected(self) -> None:
        """Validate that the client is connected and operational.

        This method checks that all necessary components are initialized
        and the connection is in a valid state for RPC operations.

        Raises
        ------
        RpcInvalidStateError
            If WebSocket or RPC channel is not initialized,
            or if the WebSocket is closed.

        Notes
        -----
        This should be called at the start of any method that requires
        an active connection (e.g., ping(), call(), reader()).
        """
        if self.ws is None:
            raise RpcInvalidStateError(
                "WebSocket not initialized. "
                "Call __aenter__() or await client.__connect__() first to establish connection."
            )
        if self.channel is None:
            raise RpcInvalidStateError(
                "RPC channel not initialized. "
                "Call __aenter__() or await client.__connect__() first to establish connection."
            )
        # Check if WebSocket is closed (websockets library sets .closed attribute)
        if getattr(self.ws, "closed", False):
            raise RpcInvalidStateError(
                "WebSocket connection is closed. "
                "Create a new WebSocketRpcClient instance to reconnect."
            )

    async def _attempt_reconnection(self) -> None:
        """Attempt to reconnect with exponential backoff.

        This method is called when a connection is lost and auto_reconnect is enabled.
        It will attempt to reconnect up to max_reconnect_attempts times, using
        exponential backoff with a maximum delay of 60 seconds between attempts.

        Notes
        -----
        The reconnection process:

        1. Checks if already reconnecting (prevents concurrent reconnection attempts)
        2. For each attempt up to max_reconnect_attempts:

           a. Calculates delay with exponential backoff (capped at 60s)
           b. Waits for the calculated delay
           c. Attempts to call __connect__()
           d. On success: logs success, resets attempt counter, returns
           e. On failure: logs warning, increments attempt counter, continues

        3. If all attempts exhausted: logs error and calls close()

        No exceptions are raised - all errors are logged and handled internally.
        If all reconnection attempts fail, the client will call close().
        """
        # Prevent concurrent reconnection attempts using a lock
        # to make the check-then-set operation atomic.
        # Note: We only hold the lock for the flag check/set operation,
        # not for the entire reconnection process (which could take minutes).
        # This is intentional - holding the lock longer would block other operations.
        async with self._reconnect_lock:
            if self._is_reconnecting:
                logger.debug(
                    "Reconnection already in progress, skipping duplicate attempt"
                )
                return

            self._is_reconnecting = True

        # Reconnection logic runs outside lock to avoid blocking for minutes
        # The _is_reconnecting flag prevents concurrent reconnection attempts
        try:
            for attempt in range(self.config.connection.max_reconnect_attempts):
                # Calculate delay with exponential backoff, capped at 60 seconds
                base_delay = self.config.connection.reconnect_delay * (2**attempt)
                capped_delay = min(base_delay, 60.0)  # Maximum 60 seconds

                # Add 0-25% jitter to prevent thundering herd problem
                # when multiple clients reconnect simultaneously
                jitter = random.uniform(0, 0.25 * capped_delay)
                delay = capped_delay + jitter

                logger.info(
                    "Reconnecting in %.1fs (attempt %d/%d)",
                    delay,
                    attempt + 1,
                    self.config.connection.max_reconnect_attempts,
                )
                await asyncio.sleep(delay)

                try:
                    # Attempt to reconnect first
                    await self.__connect__()

                    # Only reset connection state after successful connection
                    # If connection fails, flags remain correct for retry
                    self._connection_closed.clear()
                    self._closing = False
                    self._close_code = None  # Reset close diagnostics
                    self._close_reason = None

                    # Reconnection successful
                    logger.info(
                        "Reconnection successful after %d attempt(s)", attempt + 1
                    )
                    self._reconnect_attempt = 0  # Reset attempt counter on success
                    return

                except (
                    ConnectionRefusedError,
                    ConnectionClosedError,
                    ConnectionClosedOK,
                    InvalidStatus,
                    WebSocketException,
                    OSError,
                ) as e:
                    # Expected connection errors - log and retry
                    logger.warning(
                        "Reconnection attempt %d failed: %s: %s",
                        attempt + 1,
                        type(e).__name__,
                        e,
                    )
                    self._reconnect_attempt = attempt + 1

                except (ValidationError, ValueError, TypeError, RuntimeError) as e:
                    # Configuration or state errors - these won't be fixed by retrying
                    logger.error(
                        f"Reconnection attempt {attempt + 1} failed with non-retryable error: "
                        f"{type(e).__name__}: {e}"
                    )
                    break  # Stop trying, these errors won't be fixed by reconnecting

                except Exception as e:
                    # Unexpected errors - log and continue trying
                    logger.exception(
                        f"Reconnection attempt {attempt + 1} failed with unexpected error: "
                        f"{type(e).__name__}: {e}"
                    )

            # All reconnection attempts exhausted
            logger.error(
                f"All {self.config.connection.max_reconnect_attempts} reconnection attempts exhausted. "
                f"Connection permanently closed."
            )
            await self.close()

        finally:
            self._is_reconnecting = False

    def _prepare_connection_kwargs(self) -> dict[str, Any]:
        """Prepare WebSocket connection parameters.

        Returns
        -------
        dict[str, Any]
            Connection kwargs with all configured parameters.
        """
        connect_kwargs = self.connect_kwargs.copy()

        # Add subprotocols to connect_kwargs if configured
        if self.config.websocket and self.config.websocket.subprotocols:
            connect_kwargs["subprotocols"] = self.config.websocket.subprotocols
            logger.debug(
                f"Creating WebSocket connection to {self.uri} with subprotocols: "
                f"{self.config.websocket.subprotocols}"
            )
        else:
            logger.debug(
                f"Creating WebSocket connection to {self.uri} without subprotocol negotiation"
            )

        # Add compression to connect_kwargs if configured
        # The websockets library supports permessage-deflate extension natively
        if self.config.websocket and self.config.websocket.compression:
            connect_kwargs["compression"] = self.config.websocket.compression
            logger.debug(
                f"Enabling WebSocket compression: {self.config.websocket.compression} "
                f"(threshold: {self.config.websocket.compression_threshold} bytes)"
            )
        else:
            # Explicitly disable compression if not configured
            connect_kwargs["compression"] = None
            logger.debug("WebSocket compression disabled")

        # Add ping_timeout if configured (prevents mismatch with ping_interval)
        if self.config.websocket and self.config.websocket.ping_timeout is not None:
            connect_kwargs["ping_timeout"] = self.config.websocket.ping_timeout
            logger.debug(
                f"Setting ping_timeout to {self.config.websocket.ping_timeout}s"
            )

        # Set max_size at protocol level to reject oversized messages early
        # This prevents DoS attacks via large payloads
        if self._max_message_size is not None:
            connect_kwargs["max_size"] = self._max_message_size
            logger.debug(
                f"Setting WebSocket max_size to {self._max_message_size} bytes"
            )

        logger.debug(f"Connection parameters: {connect_kwargs}")
        return connect_kwargs

    async def _establish_websocket(self, connect_kwargs: dict[str, Any]) -> Any:
        """Establish WebSocket connection with optional compression fallback.

        Parameters
        ----------
        connect_kwargs : dict[str, Any]
            Connection parameters.

        Returns
        -------
        Any
            Connected WebSocket instance.

        Raises
        ------
        Exception
            Various exceptions from websockets.connect().
        """
        compression_enabled = connect_kwargs.get("compression") is not None

        # Try to connect with compression first, fallback without if negotiation fails
        raw_ws = None
        if compression_enabled:
            try:
                raw_ws = await websockets.connect(self.uri, **connect_kwargs)
            except Exception as e:
                # Check if this is a compression negotiation failure
                # websockets library may raise various exceptions for extension negotiation failures
                error_msg = str(e).lower()
                if (
                    "extension" in error_msg
                    or "compression" in error_msg
                    or "deflate" in error_msg
                ):
                    logger.warning(
                        f"Compression negotiation failed: {e}. "
                        f"Retrying without compression..."
                    )
                    # Retry without compression
                    connect_kwargs["compression"] = None
                    raw_ws = await websockets.connect(self.uri, **connect_kwargs)
                else:
                    # Not a compression error, re-raise
                    raise

        # If we didn't try compression or it succeeded, connect normally
        if raw_ws is None:
            raw_ws = await websockets.connect(self.uri, **connect_kwargs)

        return raw_ws

    async def _validate_subprotocol(self, raw_ws: Any) -> None:
        """Validate negotiated subprotocol matches configuration.

        Parameters
        ----------
        raw_ws : Any
            Raw WebSocket connection.

        Raises
        ------
        ValueError
            If subprotocol mismatch detected.
        """
        if self.config.websocket and self.config.websocket.subprotocols:
            negotiated = getattr(raw_ws, "subprotocol", None)

            if negotiated is None:
                logger.warning(
                    f"Server did not negotiate any subprotocol. "
                    f"Requested: {self.config.websocket.subprotocols}"
                )
            elif negotiated not in self.config.websocket.subprotocols:
                logger.error(
                    f"Subprotocol mismatch: requested {self.config.websocket.subprotocols}, "
                    f"got {negotiated}"
                )
                await raw_ws.close()
                raise ValueError(
                    f"Server negotiated unsupported subprotocol: {negotiated}"
                )
            else:
                logger.info(f"WebSocket subprotocol negotiated: {negotiated}")
        else:
            # Log negotiated subprotocol even if we didn't request one
            if hasattr(raw_ws, "subprotocol") and raw_ws.subprotocol:
                logger.debug(f"WebSocket subprotocol negotiated: {raw_ws.subprotocol}")
            else:
                logger.debug("No WebSocket subprotocol negotiated")

    async def _initialize_rpc_layer(self, raw_ws: Any) -> None:
        """Initialize RPC layer with serialization and channel setup.

        Parameters
        ----------
        raw_ws : Any
            Raw WebSocket connection.

        Raises
        ------
        ValidationError
            If validation fails during initialization.
        ValueError
            If configuration values are invalid.
        TypeError
            If type errors occur during initialization.
        """
        logger.debug(f"Wrapping WebSocket with {self._serializing_socket_cls.__name__}")
        # Conditionally pass max_message_size to prevent DoS via large payloads
        # Some serializers (e.g., BinarySerializingWebSocket) don't support this parameter
        if self._max_message_size is not None:
            try:
                self.ws = self._serializing_socket_cls(
                    raw_ws, max_message_size=self._max_message_size
                )  # type: ignore[call-arg]
            except TypeError as e:
                # Serializer doesn't support max_message_size parameter
                if "max_message_size" in str(e):
                    logger.warning(
                        f"{self._serializing_socket_cls.__name__} doesn't support max_message_size parameter, "
                        f"using without size limit"
                    )
                    self.ws = self._serializing_socket_cls(raw_ws)  # type: ignore[call-arg]
                else:
                    raise
        else:
            self.ws = self._serializing_socket_cls(raw_ws)

        # Create RPC channel with production hardening parameters:
        # - max_pending_requests: Prevents memory exhaustion from request flooding
        # - max_connection_duration: Auto-closes long-lived connections for rotation
        # - debug_config: Controls error disclosure to prevent information leakage
        # - subprotocol: Store negotiated WebSocket subprotocol for runtime checks
        negotiated_subprotocol = (
            raw_ws.subprotocol if hasattr(raw_ws, "subprotocol") else None
        )
        self.channel = RpcChannel(
            self.methods,
            self.ws,
            default_response_timeout=self._default_response_timeout,
            max_pending_requests=self._max_pending_requests,
            max_connection_duration=self._max_connection_duration,
            debug_config=self.config.connection.debug,
            subprotocol=negotiated_subprotocol,
            connect_callbacks=self._on_connect,
            disconnect_callbacks=self._on_disconnect,
        )

    async def _start_background_tasks(self, raw_ws: Any) -> None:
        """Start background tasks (reader, keepalive) and verify connection.

        Parameters
        ----------
        raw_ws : Any
            Raw WebSocket connection.

        Raises
        ------
        Exception
            If tasks fail to start or connection verification fails.
        """
        # Start background message reader task (runs continuously until closed)
        self._read_task = asyncio.create_task(self.reader())

        # Initialize and start keep-alive if enabled (interval > 0)
        # Keep-alive detects unresponsive connections via periodic pings
        # Protocol pings (WebSocket ping/pong frames) are 80-90% more efficient
        # than RPC pings but RPC pings test the full message processing pipeline
        if self.config.keepalive.enabled:
            self._keepalive = RpcKeepalive(
                interval=self.config.keepalive.interval,
                max_consecutive_failures=self.config.keepalive.max_consecutive_failures,
                websocket=raw_ws,  # Raw WebSocket for protocol-level pings
                close_fn=self.close,  # Bound method - triggers cleanup
                use_protocol_ping=self.config.keepalive.use_protocol_ping,
                ping_fn=self.ping,  # Bound method - fallback for RPC pings
            )
            self._keepalive.start()

        # Verify the RPC layer is working (not just socket connectivity)
        # This performs actual RPC pings with retries to ensure bidirectional communication
        await self.wait_on_rpc_ready()

    async def __connect__(self) -> WebSocketRpcClient:
        """Internal method to establish the WebSocket connection and initialize the RPC channel.

        This performs the actual connection steps:

        1. Create WebSocket connection
        2. Wrap it with serialization
        3. Set up the RPC channel
        4. Start reading messages
        5. Start keep-alive if enabled
        6. Check that RPC layer is responsive
        7. Trigger connect callbacks

        Returns
        -------
        WebSocketRpcClient
            Self reference for chaining.

        Raises
        ------
        ConnectionRefusedError
            If the server refuses the connection.
        ConnectionClosedError
            If the connection is closed during initialization.
        ConnectionClosedOK
            If the connection is closed gracefully.
        InvalidStatus
            If the server returns an invalid HTTP status code.
        WebSocketException
            For other WebSocket-related errors.
        OSError
            For network-level errors.
        ValidationError
            For data validation errors during connection.
        ValueError
            For configuration or state errors.
        TypeError
            For type-related errors during initialization.
        RuntimeError
            For unexpected state errors.
        """
        try:
            logger.info("Connecting to %s...", self.uri)

            # Step 1: Create WebSocket connection with subprotocol negotiation
            connect_kwargs = self._prepare_connection_kwargs()
            raw_ws = await self._establish_websocket(connect_kwargs)

            # Validate negotiated subprotocol
            await self._validate_subprotocol(raw_ws)

            # Step 2-4: Initialize RPC layer (wrap socket, create channel, register handlers)
            # IMPORTANT: We use nested try-except blocks to ensure proper cleanup at each stage.
            # If RPC initialization fails, we need to close the raw WebSocket before re-raising.
            try:
                await self._initialize_rpc_layer(raw_ws)

            except (ValidationError, ValueError, TypeError) as e:
                # RPC initialization failed (bad config, validation error, etc.)
                # Clean up partial state to prevent resource leaks
                logger.error("RPC initialization failed: %s: %s", type(e).__name__, e)
                await self._cleanup_partial_init()
                raise

            # Step 5-6: Start tasks and verify connection
            # CRITICAL: These tasks depend on the channel being initialized above.
            # If any fail, we must clean up ALL resources (ws, channel, tasks).
            try:
                await self._start_background_tasks(raw_ws)

            except Exception as e:
                # Post-initialization failure (reader start failed, ping timeout, etc.)
                # Must clean up ws, channel, and any started tasks
                logger.error(
                    "Connection verification failed: %s: %s", type(e).__name__, e
                )
                await self._cleanup_partial_init()
                raise

            # Reset keepalive failure counter on new connection
            if self._keepalive is not None:
                self._keepalive.reset_failures()

            # Step 7: Trigger user-provided connect callbacks
            # POLICY: User callback failures are logged but DON'T fail the connection.
            # This prevents buggy user code from breaking the connection lifecycle.
            # If you need strict callback handling, uncomment the raise below.
            try:
                assert self.channel is not None  # Guaranteed by successful init above
                await self.channel.on_connect()
            except Exception as e:
                # User callback raised an exception - log but connection succeeds
                logger.error("Connect callback failed: %s: %s", type(e).__name__, e)
                # Uncomment to enforce strict callback handling:
                # raise

            return self

        except ConnectionRefusedError:
            logger.info("RPC connection was refused by server")
            raise
        except ConnectionClosedError:
            logger.info("RPC connection lost")
            raise
        except ConnectionClosedOK:
            logger.info("RPC connection closed")
            raise
        except InvalidStatus as err:
            status_code = getattr(err, "status_code", None) or getattr(
                err, "code", None
            )
            logger.info(f"RPC WebSocket failed with invalid status code {status_code}")
            raise
        except WebSocketException as err:
            logger.info(f"RPC WebSocket failed with {err}")
            raise
        except OSError as err:
            logger.info(f"RPC Connection failed - {err}")
            raise
        except (ValidationError, ValueError, TypeError, RuntimeError) as err:
            # Catch data validation and processing errors during connection
            logger.exception(f"RPC Error: {type(err).__name__}")
            raise

    async def _cleanup_partial_init(self) -> None:
        """Clean up resources after a failed connection attempt.

        Notes
        -----
        This method is async to properly await task cancellations.
        """
        if self.ws is not None:
            with suppress(Exception):
                await self.ws.close()

        if self.channel is not None:
            with suppress(Exception):
                await self.channel.close()

        # Cancel tasks (now async)
        await self.cancel_tasks()

    async def __aenter__(self) -> WebSocketRpcClient:
        """Async context manager entry.

        Establishes connection with retry logic if configured.

        Returns
        -------
        WebSocketRpcClient
            The connected client instance.
        """
        connect_with_retry = self._retry_manager.wrap(self.__connect__)
        return await connect_with_retry()

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        """Async context manager exit.

        Ensures clean shutdown of the connection and associated resources.
        """
        await self.close()

    async def close(self) -> None:
        """Close the WebSocket connection and clean up resources.

        This method is idempotent and can be safely called multiple times.

        Notes
        -----
        This method performs the following steps:

        1. Checks if already closing (prevents race conditions)
        2. Closes the WebSocket connection if open
        3. Notifies disconnect handlers
        4. Cancels background tasks
        """
        # IDEMPOTENCY: Check if already closing to prevent duplicate cleanup
        # This is critical because close() can be called from:
        # - User code (explicit close)
        # - Keepalive (on ping failure)
        # - Reader task (on connection error)
        # - Context manager __aexit__
        # Use lock to ensure atomic check-and-set of closing flag
        async with self._close_lock:
            if self._closing:
                logger.debug("Close already in progress, skipping duplicate call")
                return

            self._closing = True

        logger.info("Closing RPC client...")

        # Signal that connection is closed (unblocks any wait_on_reader() calls)
        self._connection_closed.set()

        # Close underlying WebSocket (suppress errors since connection may already be broken)
        if self.ws is not None:
            with suppress(RuntimeError, ConnectionClosed, WebSocketException):
                await self.ws.close()

        # Trigger disconnect handlers and close channel (but only if not already closed)
        # This check prevents double-calling disconnect callbacks
        if self.channel and not self.channel.is_closed():
            try:
                # Notify user-provided disconnect handlers
                await self.channel.on_disconnect()
                # Close channel (triggers promise cleanup, stops duration watchdog)
                await self.channel.close()
            except (RuntimeError, ValueError, TypeError, AttributeError) as e:
                # POLICY: Don't raise during cleanup - log and continue
                # We want to clean up all resources even if one step fails
                logger.exception(f"Error during channel closure: {type(e).__name__}")

        # Cancel background tasks (keepalive, reader)
        await self.cancel_tasks()

    async def cancel_tasks(self) -> None:
        """Cancel all background tasks (keep-alive and reader) and wait for cleanup.

        Notes
        -----
        This method is async to properly await task cancellations, ensuring
        clean shutdown without warnings about un-awaited coroutines.
        """
        # Stop keep-alive if enabled
        if self._keepalive is not None:
            await self._keepalive.stop()

        # Stop reader task
        await self.cancel_reader_task()

    async def cancel_reader_task(self) -> None:
        """Cancel the reader task if it exists and wait for cancellation to complete.

        Notes
        -----
        This ensures proper cleanup and prevents warnings about un-awaited
        cancellation exceptions.
        """
        if self._read_task is not None:
            logger.debug("Cancelling reader task...")
            self._read_task.cancel()

            # Wait for cancellation to complete (suppress CancelledError)
            with suppress(asyncio.CancelledError):
                await self._read_task

            self._read_task = None
            logger.debug("Reader task cancelled successfully")

    async def reader(self) -> None:
        """Background task that continuously reads messages from the WebSocket.

        This task runs until the connection is closed or cancelled.
        Each received message is processed through the RPC channel.

        Raises
        ------
        RpcInvalidStateError
            If WebSocket or channel is not properly initialized.

        Notes
        -----
        Implements two layers of timeout protection (both optional):

        1. message_receive_timeout (config): Prevents Slowloris-style attacks where
           malicious servers send partial frames slowly. Default is None (disabled).
        2. idle_timeout (config): Detects stale connections where server crashes
           without sending proper TCP FIN/RST. Default is None (no idle detection).

        If both timeouts are configured, uses the minimum value for efficiency.
        If neither is configured, waits indefinitely for messages (relies on
        WebSocket protocol-level ping/pong for health checking).
        """
        # Validate connection state before starting reader loop
        self._validate_connected()

        # After validation, ws and channel are guaranteed to be non-None
        # These runtime checks protect against -O optimization flag that disables assertions
        if self.ws is None:
            raise RpcInvalidStateError("WebSocket must be connected")
        if self.channel is None:
            raise RpcInvalidStateError("Channel must be initialized")

        # Track last message time for idle detection
        import time

        last_message_time = time.monotonic()

        try:
            # Main reader loop - continuously read and process messages
            while True:
                try:
                    # Calculate effective timeout based on configuration
                    # Use the minimum of message_receive_timeout and idle_timeout
                    # If both are None, wait indefinitely
                    msg_timeout = self.config.connection.message_receive_timeout
                    idle_timeout = self.config.connection.idle_timeout

                    timeouts = [t for t in [msg_timeout, idle_timeout] if t is not None]
                    effective_timeout = min(timeouts) if timeouts else None

                    if effective_timeout is not None:
                        # Add timeout wrapper to prevent Slowloris attacks and detect idle connections
                        # Slowloris: malicious server sends partial WebSocket frames slowly
                        # to tie up client resources without completing messages
                        raw_message = await asyncio.wait_for(
                            self.ws.recv(), timeout=effective_timeout
                        )
                    else:
                        # No timeout configured - wait indefinitely
                        raw_message = await self.ws.recv()

                    logger.debug("Received message via reader")
                    last_message_time = time.monotonic()  # Update activity timestamp
                    await self.channel.on_message(raw_message)

                except asyncio.TimeoutError:
                    # Message receive timeout - possible Slowloris attack or network issue
                    # Check if this is idle timeout (configured) or message receive timeout
                    elapsed = time.monotonic() - last_message_time

                    if (
                        self.config.connection.idle_timeout is not None
                        and elapsed >= self.config.connection.idle_timeout
                    ):
                        # Idle timeout: no activity for configured duration
                        # This detects stale connections where server crashed without
                        # sending TCP FIN/RST (power failure, firewall drop, etc.)
                        logger.error(
                            f"Connection idle timeout ({self.config.connection.idle_timeout}s). "
                            f"No messages received for {elapsed:.1f}s. Closing stale connection."
                        )
                    elif self.config.connection.message_receive_timeout is not None:
                        # Message receive timeout: took too long to receive a single message
                        # This is a Slowloris-style attack or severe network degradation
                        logger.error(
                            f"Message receive timeout ({self.config.connection.message_receive_timeout}s). "
                            f"Possible slow-read attack or network issue."
                        )
                    else:
                        # Unexpected timeout (shouldn't happen if effective_timeout was None)
                        logger.error("Unexpected timeout during message receive.")

                    # Either way, close the connection
                    asyncio.create_task(self.close())
                    break

        except asyncio.CancelledError:
            # Normal cancellation during shutdown (from cancel_reader_task())
            # Signal that connection is closed but don't trigger full close()
            # because close() already cancelled us
            logger.info("RPC read task was cancelled.")
            self._connection_closed.set()

        except ConnectionClosed as e:
            # WebSocket connection lost (server closed, network error, etc.)
            # Extract close code and reason for diagnostics
            # The ConnectionClosed exception may have rcvd attribute with Close object
            close_info = getattr(e, "rcvd", None)
            if close_info is not None:
                self._close_code = getattr(close_info, "code", None)
                self._close_reason = getattr(close_info, "reason", None)

            # Log with close code and reason for better debugging
            if self._close_code is not None:
                # Special handling for code 1012 (Service Restart)
                # This is an expected, recoverable condition during server maintenance
                if self._close_code == WS_CLOSE_CODE_SERVICE_RESTART:
                    logger.info(
                        "Server restart detected (close code %d: %s). "
                        "This is a normal operational event. Reconnection recommended.",
                        WS_CLOSE_CODE_SERVICE_RESTART,
                        self._close_reason or "Service Restart",
                    )
                else:
                    logger.info(
                        "Connection was terminated. Close code: %d, reason: %s",
                        self._close_code,
                        self._close_reason or "(no reason provided)",
                    )
            else:
                logger.info("Connection was terminated.")

            # Signal closed state
            self._connection_closed.set()

            # WebSocket close codes that indicate permanent failures (DO NOT reconnect)
            # See RFC 6455 Section 7.4: https://www.rfc-editor.org/rfc/rfc6455.html#section-7.4
            #
            # Retryable codes (not in this set) include:
            # - 1000: Normal closure (client/server intentional close)
            # - 1001: Going away (server shutting down, page navigating away)
            # - 1006: Abnormal closure (network failure, no close frame received)
            # - 1012: Service Restart (server restarting, reconnection encouraged)
            non_retryable_codes = {
                WS_CLOSE_CODE_PROTOCOL_ERROR,  # Protocol Error - implementation/protocol violation
                WS_CLOSE_CODE_UNSUPPORTED_DATA,  # Unsupported Data - data type not acceptable
                WS_CLOSE_CODE_INVALID_DATA,  # Invalid frame payload data - inconsistent/malformed data
                WS_CLOSE_CODE_POLICY_VIOLATION,  # Policy Violation - generic policy violation
                WS_CLOSE_CODE_INTERNAL_ERROR,  # Internal Error - unexpected server condition
            }

            # Check if close code indicates a permanent failure
            if self._close_code in non_retryable_codes:
                logger.error(
                    "Connection closed with non-retryable code %d: %s. "
                    "This indicates a permanent failure (protocol error, policy violation, etc.). "
                    "Will NOT attempt reconnection.",
                    self._close_code,
                    self._close_reason,
                )
                # Close permanently - server explicitly rejected connection
                asyncio.create_task(self.close())
                return

            # Decide whether to reconnect or close permanently
            if self.config.connection.auto_reconnect:
                # Attempt automatic reconnection with exponential backoff
                logger.info("Auto-reconnect enabled, attempting to reconnect...")
                asyncio.create_task(self._attempt_reconnection())
            else:
                # No auto-reconnect - trigger full cleanup
                # Use create_task to avoid reader task waiting for its own cancellation
                asyncio.create_task(self.close())

        except (ValidationError, ValueError, TypeError, RuntimeError, KeyError) as e:
            # Message processing errors indicate protocol violations or bugs:
            # - ValidationError: Invalid JSON-RPC 2.0 message structure
            # - ValueError/TypeError: Bad data types or values
            # - RuntimeError: Unexpected state errors
            # - KeyError: Missing required fields in message
            # These are FATAL - we can't recover, so we signal closed and re-raise
            logger.exception(f"RPC reader task failed: {type(e).__name__}")
            self._connection_closed.set()
            raise

    async def wait_on_rpc_ready(self) -> None:
        """Verify that the RPC channel is ready by sending test pings.

        Notes
        -----
        This confirms not just socket connectivity but RPC functionality.
        Makes multiple attempts with timeout before giving up.
        """
        # Connection verification settings
        max_attempts = 5  # Maximum number of verification attempts
        wait_timeout = 1.0  # Timeout in seconds for each ping attempt

        received_response = None
        attempt_count = 0

        # RETRY PATTERN: Try up to max_attempts times to verify RPC readiness
        # This is necessary because:
        # 1. Server may need time to process the connection
        # 2. Reader task may not be fully started yet
        # 3. Network conditions may cause transient failures
        # If all attempts fail, we raise the last exception
        while received_response is None and attempt_count < max_attempts:
            try:
                logger.debug(
                    f"RPC ready check attempt {attempt_count + 1}/{max_attempts}"
                )
                # Send a ping with short timeout (1 second by default)
                # This verifies bidirectional RPC communication
                received_response = await asyncio.wait_for(self.ping(), wait_timeout)
                logger.debug(f"RPC ready check succeeded: {received_response}")

            except asyncio.TimeoutError:
                # Ping timed out - server may be slow or reader not fully started
                # Increment counter and retry (silent failure - we'll retry)
                attempt_count += 1
                logger.debug(f"RPC ready check timed out (attempt {attempt_count})")

            except (
                RpcChannelClosedError,
                RpcBackpressureError,
                RpcInvalidStateError,
                ConnectionError,
                OSError,
            ) as e:
                # Expected errors during connection establishment:
                # - RpcChannelClosedError: Channel closed during ping
                # - RpcBackpressureError: Too many pending requests (rare during init)
                # - RpcInvalidStateError: Channel not fully initialized
                # - ConnectionError/OSError: Network-level issues
                # NOTE: We don't catch system exceptions (KeyboardInterrupt, SystemExit)
                logger.warning(f"RPC ready check failed: {e}")
                attempt_count += 1

    async def ping(self) -> Any:
        """Send a ping request to the server and wait for a response.

        Returns
        -------
        Any
            The response object from the server's ping method.

        Raises
        ------
        RpcInvalidStateError
            If the RPC channel is not initialized.
        """
        # Validate that channel is initialized
        self._validate_connected()

        logger.debug("Pinging server...")
        answer = await self.channel.other.ping()  # type: ignore[union-attr]
        logger.debug(f"Got ping response: {answer}")
        return answer

    async def wait_on_reader(self) -> None:
        """Wait for the reader task to complete.

        Notes
        -----
        Useful for graceful shutdown or waiting for processing to finish.
        """
        if self._read_task:
            try:
                await self._read_task
            except asyncio.CancelledError:
                logger.info("RPC Reader task was cancelled.")

    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Call a remote method on the server and wait for the response.

        Parameters
        ----------
        name : str
            Name of the method to call (as defined on the server's RpcMethods).
        args : dict[str, Any] | None, optional
            Keyword arguments to pass to the remote method.
        timeout : float | None, optional
            Optional custom timeout for this specific call.

        Returns
        -------
        Any
            The result returned by the remote method.

        Raises
        ------
        RpcInvalidStateError
            If the RPC channel is not initialized.
        RpcChannelClosedError
            If the connection is closed.
        asyncio.TimeoutError
            If the call times out.
        """
        # Validate that channel is initialized
        self._validate_connected()

        args = args or {}
        return await self.channel.call(name, args, timeout=timeout)  # type: ignore[union-attr]

    @property
    def other(self) -> Any:
        """Proxy object for calling remote methods.

        This allows convenient attribute-style access to remote methods::

            result = await client.other.remote_method(param1=value1)

        Returns
        -------
        RpcCaller
            Proxy object for remote method calls.
        """
        return self.channel.other if self.channel else None

    @property
    def is_connected(self) -> bool:
        """Check if the client is currently connected.

        Returns
        -------
        bool
            True if connected and operational, False otherwise.
        """
        return (
            self.ws is not None
            and not getattr(self.ws, "closed", False)
            and self.channel is not None
            and not self.channel.is_closed()
            and self._read_task is not None
            and not self._read_task.done()
        )

    @property
    def is_closed_event(self) -> asyncio.Event:
        """Event that is set when the connection is closed.

        This event is set when:

        - The reader task detects a ConnectionClosed exception
        - The reader task is cancelled
        - The close() method is called
        - Any exception occurs in the reader task

        Returns
        -------
        asyncio.Event
            Event that will be set when the connection closes.
        """
        return self._connection_closed

    @property
    def close_code(self) -> int | None:
        """WebSocket close code from the last connection closure.

        Returns
        -------
        int | None
            The close code if available, None otherwise.
            Common codes include:

            - 1000: Normal closure
            - 1001: Going away
            - 1002: Protocol error
            - 1003: Unsupported data
            - 1006: Abnormal closure (no close frame received)
            - 1012: Service restart (server restarting, reconnection recommended)
        """
        return self._close_code

    @property
    def close_reason(self) -> str | None:
        """WebSocket close reason from the last connection closure.

        Returns
        -------
        str | None
            The close reason string if available, None otherwise.
        """
        return self._close_reason
