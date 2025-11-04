"""
WebSocketRpcClient module provides a client that connects to a WebSocketRpcEndpoint
via websocket and enables bi-directional RPC calls.
"""

from __future__ import annotations

import asyncio
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

# Type alias for error callbacks (will be used in future error handling refactor)
OnErrorCallback = OnDisconnectCallback

logger = get_logger(__name__)


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
        """
        Initialize the WebSocketRpcClient.

        Args:
            uri: Server URI to connect to (e.g., 'ws://localhost/ws/client1')
            methods: RPC methods to expose to the server. Defaults to an empty
                RpcMethodsBase.
            config: Configuration object for all client behavior. If None, uses
                default configuration. Use WebSocketRpcClientConfig.production_defaults()
                or .development_defaults() for common presets.
            on_connect: Callbacks executed when connection is established.
            on_disconnect: Callbacks executed when connection is lost.
            on_error: Callbacks executed when errors occur (currently maps to on_disconnect).
            serializing_socket_cls: Class for serializing/deserializing messages.

        Usage:
            ```python
            # Using default config
            async with WebSocketRpcClient(uri, RpcUtilityMethods()) as client:
                response = await client.call("echo", {'text': "Hello World!"})
                print(response)

            # Using production config with custom settings
            from fastapi_ws_rpc.config import WebSocketRpcClientConfig, RpcKeepaliveConfig
            config = WebSocketRpcClientConfig(
                keepalive=RpcKeepaliveConfig(interval=30.0, use_protocol_ping=True)
            )
            async with WebSocketRpcClient(uri, methods, config=config) as client:
                await client.call("some_method")

            # Using preset configurations
            config = WebSocketRpcClientConfig.production_defaults()
            async with WebSocketRpcClient(uri, methods, config=config) as client:
                await client.call("some_method")
            ```
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
        # on_error currently maps to on_disconnect (will be used in future error handling refactor)
        if on_error:
            self._on_disconnect.extend(on_error)

        # Serialization
        self._serializing_socket_cls = serializing_socket_cls

    def _validate_connected(self) -> None:
        """
        Validate that the client is connected and operational.

        This method checks that all necessary components are initialized
        and the connection is in a valid state for RPC operations.

        Raises:
            RpcInvalidStateError: If WebSocket or RPC channel is not initialized,
                                 or if the WebSocket is closed.

        Usage:
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

    async def __connect__(self) -> WebSocketRpcClient:
        """
        Internal method to establish the WebSocket connection and initialize the RPC
        channel.

        This performs the actual connection steps:
        1. Create WebSocket connection
        2. Wrap it with serialization
        3. Set up the RPC channel
        4. Start reading messages
        5. Start keep-alive if enabled
        6. Check that RPC layer is responsive
        7. Trigger connect callbacks

        Returns:
            WebSocketRpcClient: Self reference for chaining.

        Raises:
            Various exceptions from websockets.connect() or during initialization.
        """
        try:
            logger.info(f"Connecting to {self.uri}...")

            # Step 1: Create WebSocket connection with subprotocol negotiation
            # This can raise connection-related exceptions
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

            logger.debug(f"Connection parameters: {connect_kwargs}")
            raw_ws = await websockets.connect(self.uri, **connect_kwargs)

            # Log negotiated subprotocol
            if hasattr(raw_ws, "subprotocol") and raw_ws.subprotocol:
                logger.info(f"WebSocket subprotocol negotiated: {raw_ws.subprotocol}")
            else:
                logger.debug("No WebSocket subprotocol negotiated")

            # Step 2-4: Initialize RPC layer (wrap socket, create channel, register handlers)
            # IMPORTANT: We use nested try-except blocks to ensure proper cleanup at each stage.
            # If RPC initialization fails, we need to close the raw WebSocket before re-raising.
            try:
                logger.debug(
                    f"Wrapping WebSocket with {self._serializing_socket_cls.__name__}"
                )
                # Conditionally pass max_message_size to prevent DoS via large payloads
                # Some serializers (e.g., BinarySerializingWebSocket) don't support this parameter
                if self._max_message_size is not None:
                    try:
                        self.ws = self._serializing_socket_cls(raw_ws, max_message_size=self._max_message_size)  # type: ignore[call-arg]
                    except TypeError as e:
                        # Serializer doesn't support max_message_size parameter
                        if "max_message_size" in str(e):
                            logger.warning(
                                f"{self._serializing_socket_cls.__name__} doesn't support max_message_size parameter, "
                                f"using without size limit"
                            )
                            self.ws = self._serializing_socket_cls(raw_ws)
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

            except (ValidationError, ValueError, TypeError) as e:
                # RPC initialization failed (bad config, validation error, etc.)
                # Clean up partial state to prevent resource leaks
                logger.error(f"RPC initialization failed: {type(e).__name__}: {e}")
                await self._cleanup_partial_init()
                raise

            # Step 5-6: Start tasks and verify connection
            # CRITICAL: These tasks depend on the channel being initialized above.
            # If any fail, we must clean up ALL resources (ws, channel, tasks).
            try:
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

            except Exception as e:
                # Post-initialization failure (reader start failed, ping timeout, etc.)
                # Must clean up ws, channel, and any started tasks
                logger.error(f"Connection verification failed: {type(e).__name__}: {e}")
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
                await self.channel.on_connect()
            except Exception as e:
                # User callback raised an exception - log but connection succeeds
                logger.error(f"Connect callback failed: {type(e).__name__}: {e}")
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
        """
        Clean up resources after a failed connection attempt.

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
        """
        Async context manager entry.

        Establishes connection with retry logic if configured.

        Returns:
            WebSocketRpcClient: The connected client instance.
        """
        connect_with_retry = self._retry_manager.wrap(self.__connect__)
        return await connect_with_retry()

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        """
        Async context manager exit.

        Ensures clean shutdown of the connection and associated resources.
        """
        await self.close()

    async def close(self) -> None:
        """
        Close the WebSocket connection and clean up resources.

        This method is idempotent and can be safely called multiple times.

        This method:
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
        # Without this guard, we'd get duplicate callbacks and task cancellation errors
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
        """
        Cancel all background tasks (keep-alive and reader) and wait for cleanup.

        This method is async to properly await task cancellations, ensuring
        clean shutdown without warnings about un-awaited coroutines.
        """
        # Stop keep-alive if enabled
        if self._keepalive is not None:
            await self._keepalive.stop()

        # Stop reader task
        await self.cancel_reader_task()

    async def cancel_reader_task(self) -> None:
        """
        Cancel the reader task if it exists and wait for cancellation to complete.

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
        """
        Background task that continuously reads messages from the WebSocket.

        This task runs until the connection is closed or cancelled.
        Each received message is processed through the RPC channel.

        Raises:
            RpcInvalidStateError: If WebSocket or channel is not properly initialized.
        """
        # Validate connection state before starting reader loop
        self._validate_connected()

        # After validation, ws and channel are guaranteed to be non-None
        # These runtime checks protect against -O optimization flag that disables assertions
        if self.ws is None:
            raise RpcInvalidStateError("WebSocket must be connected")
        if self.channel is None:
            raise RpcInvalidStateError("Channel must be initialized")

        try:
            # Main reader loop - continuously read and process messages
            while True:
                raw_message = await self.ws.recv()
                logger.debug("Received message via reader")
                await self.channel.on_message(raw_message)

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
                logger.info(
                    f"Connection was terminated. Close code: {self._close_code}, "
                    f"reason: {self._close_reason or '(no reason provided)'}"
                )
            else:
                logger.info("Connection was terminated.")

            # Signal closed state and trigger full cleanup
            self._connection_closed.set()
            await self.close()

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
        """
        Verify that the RPC channel is ready by sending test pings.

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
        """
        Send a ping request to the server and wait for a response.

        Returns:
            The response object from the server's ping method.

        Raises:
            RpcInvalidStateError: If the RPC channel is not initialized.
        """
        # Validate that channel is initialized
        self._validate_connected()

        logger.debug("Pinging server...")
        answer = await self.channel.other.ping()  # type: ignore[union-attr]
        logger.debug(f"Got ping response: {answer}")
        return answer

    async def wait_on_reader(self) -> None:
        """
        Wait for the reader task to complete.

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
        """
        Call a remote method on the server and wait for the response.

        Args:
            name: Name of the method to call (as defined on the server's RpcMethods)
            args: Keyword arguments to pass to the remote method
            timeout: Optional custom timeout for this specific call

        Returns:
            The result returned by the remote method

        Raises:
            RpcInvalidStateError: If the RPC channel is not initialized.
            Various exceptions from the underlying RPC channel or if the connection
            is closed.
        """
        # Validate that channel is initialized
        self._validate_connected()

        args = args or {}
        return await self.channel.call(name, args, timeout=timeout)  # type: ignore[union-attr]

    @property
    def other(self) -> Any:
        """
        Proxy object for calling remote methods.

        This allows convenient attribute-style access to remote methods:
        ```python
        result = await client.other.remote_method(param1=value1)
        ```

        Returns:
            RpcCaller: Proxy object for remote method calls
        """
        return self.channel.other if self.channel else None

    @property
    def is_connected(self) -> bool:
        """
        Check if the client is currently connected.

        Returns:
            bool: True if connected and operational, False otherwise
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
        """
        Event that is set when the connection is closed.

        This event is set when:
        - The reader task detects a ConnectionClosed exception
        - The reader task is cancelled
        - The close() method is called
        - Any exception occurs in the reader task

        Returns:
            asyncio.Event: Event that will be set when the connection closes.
        """
        return self._connection_closed

    @property
    def close_code(self) -> int | None:
        """
        WebSocket close code from the last connection closure.

        Returns:
            int | None: The close code if available, None otherwise.
                       Common codes include:
                       - 1000: Normal closure
                       - 1001: Going away
                       - 1002: Protocol error
                       - 1003: Unsupported data
                       - 1006: Abnormal closure (no close frame received)
        """
        return self._close_code

    @property
    def close_reason(self) -> str | None:
        """
        WebSocket close reason from the last connection closure.

        Returns:
            str | None: The close reason string if available, None otherwise.
        """
        return self._close_reason
